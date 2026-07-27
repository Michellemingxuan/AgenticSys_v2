"""Layer 1 — orchestrator user-message assembly (episodic + KB warmth + question)."""
from __future__ import annotations

from tools.episodic import (
    EPISODIC_TURNS, build_records, render_orchestrator_block, select_episodic,
)

# Char budget for each KP claim in the orchestrator's KB-warmth digest. This is
# a FALLBACK view: when Amem hybrid retrieval is available the orchestrator gets
# full, un-clipped claims instead (see assemble_orchestrator_input). 300
# preserves whole distiller one-liners (~150-250 chars, incl. both metrics and
# qualifiers like "not breached") rather than cutting them mid-fact; a trailing
# "…" marks any real truncation so a cut is visible, not silently misleading.
_KB_WARMTH_CLAIM_CHARS = 300


def _clip_claim(claim: str | None) -> str:
    claim = (claim or "").strip()
    if len(claim) <= _KB_WARMTH_CLAIM_CHARS:
        return claim
    return claim[:_KB_WARMTH_CLAIM_CHARS].rstrip() + "…"


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
            claim = _clip_claim(kp.get("claim"))
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


def _format_case_summary_block(summary: str) -> str:
    """Render the durable Amem case summary as the condensed 'older context'
    block (injected only past the episodic window). Empty -> "" (skipped)."""
    summary = (summary or "").strip()
    if not summary:
        return ""
    return ("[CASE SUMMARY — condensed older context for this case (turns before "
            "the recent ones shown below). Background only; the recent turns and "
            "live data are authoritative:\n" + summary + "\n]")


def _compose_framed_question(*parts: str) -> str:
    """Order: case summary (older) -> episodic (recent) -> KB warmth (topics) ->
    question. Skip empties."""
    return "\n\n".join(p for p in parts if p)


def assemble_orchestrator_input(sess, verdict, ctx, case_summary: str = "") -> str:
    """Build the orchestrator's framed user message and stash episodic records on ctx.

    Composition (broad -> specific): case summary (condensed older context, only
    past the episodic window) -> episodic (recent turns verbatim, for coreference)
    -> KB-warmth (specialist topics + claims) -> question. Returns the framed
    question string. Side effect: sets ctx._episodic_records.
    """
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
        _format_case_summary_block(case_summary),
        episodic_block, warmth_hint, verdict.redacted_question)
