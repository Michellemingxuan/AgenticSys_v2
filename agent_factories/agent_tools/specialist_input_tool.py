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


def _format_agent_case_summary_block(summary: str) -> str:
    """Render a specialist's OWN case summary (its accumulated findings across
    the case, condensed) as the 'older context' block — injected when it has
    run in more than its episodic window. Empty -> "" (skipped)."""
    summary = (summary or "").strip()
    if not summary:
        return ""
    return ("[CASE SUMMARY — your own accumulated findings for this case so far "
            "(condensed; covers turns older than the recent ones above). "
            "Background only — re-query the data for exact current values:\n"
            + summary + "\n]")


def _compose_specialist_input(episodic_block: str, kb_digest: str,
                              sub_question: str, directed_block: str = "",
                              case_summary_block: str = "") -> str:
    """Prepend the agent case summary (older accumulated findings), episodic
    slice, KB digest, and directed-variable block (each non-empty, in that
    order) before the sub-question. Directed variables sit last (nearest the
    question) as the most question-specific prefix. Byte-identical to the prior
    behavior when directed_block and case_summary_block are empty."""
    prefixes = [p for p in (case_summary_block, episodic_block, kb_digest,
                            directed_block) if p]
    if not prefixes:
        return sub_question
    return "\n\n".join(prefixes) + f"\n\n--- New question ---\n{sub_question}"


# How many representative columns to surface per concept. The concepts are
# DIRECTIONS, not an exhaustive checklist — a couple of pointers per concept is
# enough to orient the specialist; it queries what it judges relevant and can
# get_table_schema for the full set. Keeping this low is what stops the
# specialist from trending every matched column (the round-count regression).
_DIRECTED_VARS_PER_CONCEPT = 2


def _render_directed_variables(
    variables: list[dict], per_concept: int = _DIRECTED_VARS_PER_CONCEPT,
) -> str:
    """Render the §DIRECTED VARIABLES block as DIRECTIONAL hints grouped by
    concept — a few representative columns per concept, framed as starting
    points (NOT an exhaustive checklist). The orchestrator conveys 2-3 concepts
    as directions; this must not turn them into a to-do list of every matched
    column."""
    if not variables:
        return ""
    by_concept: dict[str, list[dict]] = {}
    for v in variables:
        by_concept.setdefault(v["concept"], []).append(v)
    lines = [
        "§ DIRECTED VARIABLES — directional starting points for this question "
        "(NOT a checklist: begin here, query what you judge relevant, you need "
        "not cover every one):",
    ]
    for concept, vs in by_concept.items():
        for v in vs[:per_concept]:
            thr = f"; {v['threshold_text']}" if v.get("threshold_text") else ""
            lines.append(f"[{concept}] {v['name']} — {v['description_short']}{thr}")
    return "\n".join(lines)


def assemble_specialist_input(app_ctx, name, redacted_in, concepts, catalog,
                              data_hints, logger) -> tuple[str, int]:
    """Layer 5 — build a specialist's first-call input: episodic + KB digest +
    directed variables + sub-question. Returns (contextual_in, kb_digest_n_kps)."""
    from tools.kb_tools import _active_kps, _format_kb_digest
    from tools.episodic import (
        EPISODIC_TURNS, render_specialist_block, select_specialist_episodic,
    )
    kb_digest, kps_for_name = "", []
    kb_obj = getattr(app_ctx, "_specialist_kb", None)
    # report_agent retrieves from curated report files via fs_grep /
    # fs_read_file, not the KB — so it gets no KB digest (and never the
    # cross-specialist topics, which it can't kb_lookup anyway). Its own
    # episodic slice (prior report drafts) is still injected below.
    if name != "report_agent" and isinstance(kb_obj, dict):
        kps_for_name = kb_obj.get(name, [])
        kb_digest = _format_kb_digest(kps_for_name, full_kb=kb_obj, self_name=name)
    try:
        _recs = getattr(app_ctx, "_episodic_records", None) or []
        _episodic_block = render_specialist_block(
            select_specialist_episodic(_recs, name, EPISODIC_TURNS))
    except Exception as _epi_exc:  # noqa: BLE001 — never break the specialist call
        _episodic_block = ""
        if logger is not None:
            logger.log("episodic_specialist_assembly_failed",
                       {"specialist": name, "error": repr(_epi_exc)})
    _directed_block = ""
    if concepts and catalog is not None and data_hints:
        try:
            _vars = catalog.variables_for_concepts(data_hints, concepts)
            _directed_block = _render_directed_variables(_vars)
            if _directed_block and logger is not None:
                logger.log("directed_variables_injected",
                           {"specialist": name, "concepts": concepts,
                            "count": len(_vars)})
        except Exception as _dv_exc:  # noqa: BLE001 — never break the call
            _directed_block = ""
            if logger is not None:
                logger.log("directed_variables_assembly_failed",
                           {"specialist": name, "concepts": concepts,
                            "error": repr(_dv_exc)})
    # Agent-specific case summary: this specialist's own condensed older
    # findings (built durably every consolidate tick, once it has run in >
    # EPISODIC_TURNS turns). Injected when present; report_agent has no KB/case
    # memory of its own.
    _agent_summary_block = ""
    if name != "report_agent":
        _amem = getattr(app_ctx, "_amem", None)
        _cfg = getattr(app_ctx, "_amem_cfg", None)
        _cid = getattr(app_ctx, "_case_id", None)
        if _amem is not None and _cfg is not None and _cid:
            try:
                from memory import load_case_summary
                _agent_summary_block = _format_agent_case_summary_block(
                    load_case_summary(_amem, _cfg, case_id=_cid, agent_id=name,
                                      kind="agent_case_summary"))
            except Exception as _cs_exc:  # noqa: BLE001 — never break the call
                _agent_summary_block = ""
                if logger is not None:
                    logger.log("agent_case_summary_load_failed",
                               {"specialist": name, "error": repr(_cs_exc)})
    contextual_in = _compose_specialist_input(
        _episodic_block, kb_digest, redacted_in, _directed_block,
        case_summary_block=_agent_summary_block)
    return contextual_in, (len(_active_kps(kps_for_name)) if kb_digest else 0)


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
