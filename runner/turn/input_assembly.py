"""Layer 1 — orchestrator user-message assembly (episodic + KB warmth + question)."""
from __future__ import annotations

from tools.episodic import (
    EPISODIC_TURNS, build_records, render_orchestrator_block, select_episodic,
)


def _format_kb_warmth_hint(specialist_kb: dict) -> str:
    """Build the `[KB-warmth: …]` preface the orchestrator sees on every turn
    after the first one.

    Lists each specialist with non-empty KB, its topic names, and one-line
    claims — the orchestrator uses this for:
      1. Routing: reuse warm specialists for in-domain follow-ups.
      2. Sub-question framing: reference cached data in the sub-question so
         the specialist (or the orchestrator itself) can skip re-querying.
         E.g. "TSR breached threshold in 2024-08..2024-10 (per KB). What
         strategy actions coincided with those months?"

    Returns "" when no specialist has any KPs (e.g. first turn).
    """
    if not isinstance(specialist_kb, dict) or not specialist_kb:
        return ""
    lines: list[str] = []
    for name in sorted(specialist_kb):
        kps = specialist_kb[name]
        if not kps:
            continue
        active: dict[str, dict] = {}
        for kp in kps:
            topic = kp.get("topic")
            if topic:
                active[topic] = kp
        if not active:
            continue
        topic_lines = []
        for topic, kp in active.items():
            claim = (kp.get("claim") or "")[:120]
            topic_lines.append(f"    - {topic}: {claim}")
        lines.append(f"  {name} ({len(active)} KPs):")
        lines.extend(topic_lines)
    if not lines:
        return ""
    return (
        "[KB-warmth — cached specialist knowledge from prior turns. "
        "Use topic details to anchor sub-questions and avoid redundant queries:\n"
        + "\n".join(lines)
        + "\n"
        + "Reuse warm specialists for in-domain follow-ups. "
        + "Reference specific cached findings in sub-questions when relevant.]"
    )


def _compose_framed_question(episodic_block: str, warmth_hint: str, question: str) -> str:
    """Order: episodic (coreference) -> KB warmth (topics) -> question. Skip empties."""
    return "\n\n".join(p for p in (episodic_block, warmth_hint, question) if p)


def assemble_orchestrator_input(sess, verdict, ctx, amem_block: str = "") -> str:
    """Build the orchestrator's framed user message and stash episodic records on ctx.

    When *amem_block* (Amem hybrid retrieval) is provided, it replaces the bulk
    KB-warmth dump — full-claim, relevance-ranked, no 120-char clip. Episodic is
    kept regardless (it resolves coreference against the immediate thread).
    Returns the framed question string. Side effect: sets ctx._episodic_records.
    """
    if amem_block:
        warmth_hint = ""
    else:
        warmth_hint = _format_kb_warmth_hint(sess.specialist_kb)
        if warmth_hint:
            sess.logger.log("kb_warmth_hint_emitted", {
                "turn_id": getattr(ctx, "_turn_id", None),
                "warm_specialists": [
                    {"name": n, "n_kps": len(kps)}
                    for n, kps in sess.specialist_kb.items() if kps
                ],
                "hint_length": len(warmth_hint),
            })
    try:
        episodic_window = build_records(sess.qa_cache)
        episodic_block = render_orchestrator_block(
            select_episodic(episodic_window, EPISODIC_TURNS))
    except Exception as _epi_exc:  # noqa: BLE001 — episodic assembly must never break a turn
        episodic_window, episodic_block = [], ""
        sess.logger.log("episodic_assembly_failed",
                        {"turn_id": getattr(ctx, "_turn_id", None),
                         "error": repr(_epi_exc)})
    ctx._episodic_records = episodic_window
    return _compose_framed_question(
        episodic_block, amem_block or warmth_hint, verdict.redacted_question)
