"""Layer-5 input builders for the agent_tool wrapper.

Assembles the input message passed to a wrapped specialist Agent: the episodic
slice, KB digest, and directed-variable prefixes composed ahead of the
sub-question, plus the transcript compaction that trims older tool-result
payloads from a reused specialist history.
"""
from __future__ import annotations


_SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES = 2
_ELIDED_SPECIALIST_TOOL_OUTPUT = (
    "(elided - earlier in-turn specialist tool output; rely on the latest "
    "turn context or re-query only if the value is still needed.)"
)


def _compose_specialist_input(episodic_block: str, kb_digest: str,
                              sub_question: str, directed_block: str = "") -> str:
    """Prepend episodic slice, KB digest, and directed-variable block (each
    non-empty, in that order) before the sub-question. Directed variables sit
    last (nearest the question) as the most question-specific prefix.
    Byte-identical to the prior behavior when directed_block is empty."""
    prefixes = [p for p in (episodic_block, kb_digest, directed_block) if p]
    if not prefixes:
        return sub_question
    return "\n\n".join(prefixes) + f"\n\n--- New question ---\n{sub_question}"


def _render_directed_variables(variables: list[dict]) -> str:
    """Render the compact §DIRECTED VARIABLES block from variables_for_concepts."""
    if not variables:
        return ""
    lines = ["§ DIRECTED VARIABLES (for this question — from the data catalog)"]
    for v in variables:
        thr = f"; {v['threshold_text']}" if v.get("threshold_text") else ""
        lines.append(f"[{v['concept']}] {v['name']} — {v['description_short']}{thr}")
    return "\n".join(lines)


def _compact_specialist_history(
    history: list,
    keep_recent_user_messages: int = _SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES,
) -> tuple[list, dict]:
    """Elide older tool-result payloads from a specialist transcript.

    The transcript is only reused inside the same outer turn, mainly for
    follow-up calls and retry salvage. Keeping the latest user-message window
    intact preserves local continuity while preventing earlier large data-tool
    outputs from being retained repeatedly in ``AppContext``.
    """
    stats = {"items_total": len(history) if isinstance(history, list) else 0,
             "items_elided": 0, "bytes_saved": 0}
    if not isinstance(history, list) or not history:
        return history, stats

    user_idxs = [
        i for i, item in enumerate(history)
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    if len(user_idxs) <= keep_recent_user_messages:
        return history, stats

    cutoff_idx = user_idxs[-keep_recent_user_messages]
    compacted: list = []
    for i, item in enumerate(history):
        if i >= cutoff_idx:
            compacted.append(item)
            continue
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            old_output = item.get("output", "")
            if isinstance(old_output, str) and old_output != _ELIDED_SPECIALIST_TOOL_OUTPUT:
                stub = dict(item)
                stub["output"] = _ELIDED_SPECIALIST_TOOL_OUTPUT
                compacted.append(stub)
                stats["items_elided"] += 1
                stats["bytes_saved"] += max(
                    0, len(old_output) - len(_ELIDED_SPECIALIST_TOOL_OUTPUT),
                )
                continue
        compacted.append(item)
    return compacted, stats
