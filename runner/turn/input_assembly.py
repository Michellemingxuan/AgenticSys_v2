"""Layer 1 — orchestrator user-message assembly (episodic + KP warmth + question)."""
from __future__ import annotations

from tools.episodic import (
    EPISODIC_TURNS, build_records, render_orchestrator_block, select_episodic,
)

# Char budget for each KP claim in the orchestrator's KP-warmth digest. This is
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


def _format_kp_warmth_hint(specialist_kps: dict) -> str:
    """Build the `[KP-warmth: …]` preface the orchestrator sees on every turn
    after the first one.

    Lists each specialist with non-empty KP, its topic names, and one-line
    claims — the orchestrator uses this for:
      1. Routing: reuse warm specialists for in-domain follow-ups.
      2. Sub-question framing: reference cached data in the sub-question so
         the specialist (or the orchestrator itself) can skip re-querying.
         E.g. "TSR breached threshold in 2024-08..2024-10 (per KP). What
         strategy actions coincided with those months?"

    Returns "" when no specialist has any KPs (e.g. first turn).
    """
    if not isinstance(specialist_kps, dict) or not specialist_kps:
        return ""
    lines: list[str] = []
    for name in sorted(specialist_kps):
        kps = specialist_kps[name]
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
        "[KP-warmth — cached specialist knowledge from prior turns. "
        "Use topic details to anchor sub-questions and avoid redundant queries:\n"
        + "\n".join(lines)
        + "\n"
        + "Reuse warm specialists for in-domain follow-ups. "
        + "Reference specific cached findings in sub-questions when relevant. "
        + "This block is UNDATED and unordered — it is NOT a recency signal. "
        + "Never use it to decide what a subject-less follow-up (\"think "
        + "harder\", \"it\", \"that\", \"are you sure?\") refers to: the "
        + "EPISODIC block above is the only authority on which turn came last. "
        + "A topic appearing here means a specialist once cached it, not that "
        + "it is what the reviewer is currently asking about.]"
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
    """Order: case summary (older) -> episodic (recent) -> KP warmth (topics) ->
    question. Skip empties."""
    return "\n\n".join(p for p in parts if p)


def _table_owners() -> dict[str, str]:
    """`{real table name: specialist that owns it}`, from the skills' own
    `data_hints`. Derived rather than declared twice, so adding a table to a
    skill is enough — there is no second list to keep in step."""
    owners: dict[str, str] = {}
    try:
        from skills.domain.loader import list_domain_skills, load_domain_skill
        from tools.data_tools import _resolve_real_table
    except Exception:  # noqa: BLE001
        return owners
    for name in list_domain_skills():
        skill = load_domain_skill(name)
        if not skill:
            continue
        for table in skill.data_hints or []:
            # Canonical AND real spelling — the hint carries whichever name the
            # column index reports.
            owners.setdefault(table, skill.name)
            try:
                owners.setdefault(_resolve_real_table(table), skill.name)
            except Exception:  # noqa: BLE001
                pass
    return owners


def assemble_orchestrator_input(sess, verdict, ctx, case_summary: str = "") -> str:
    """Build the orchestrator's framed user message and stash episodic records on ctx.

    Composition (broad -> specific): case summary (condensed older context, only
    past the episodic window) -> episodic (recent turns verbatim, for coreference)
    -> KP-warmth (specialist topics + claims) -> question. Returns the framed
    question string. Side effect: sets ctx._episodic_records.
    """
    warmth_hint = _format_kp_warmth_hint(sess.specialist_kps)
    if warmth_hint:
        sess.logger.log("kp_warmth_hint_emitted", {
            "turn_id": getattr(ctx, "_turn_id", None),
            "warm_specialists": [
                {"name": n, "n_kps": len(kps)}
                for n, kps in sess.specialist_kps.items() if kps
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
    # LAST, after the question: a bare variable name reads as a proper noun.
    # "how is intoop" was answered as though INTOOP were the customer ("Intoop
    # is a high-utilization commercial customer with 3 cards…") and dispatched
    # as a generic case overview, never touching `oop_interaction_max`. The
    # screen already resolved the name against this case's data, so this hands
    # over what is known rather than asking the orchestrator to guess again.
    variable_hint = ""
    if getattr(verdict, "named_variables", None):
        try:
            from tools.data_tools import variable_routing_hint
            variable_hint = variable_routing_hint(
                verdict.redacted_question, owners=_table_owners())
        except Exception:  # noqa: BLE001 — never break a turn over a hint
            variable_hint = ""
        if variable_hint:
            sess.logger.log("variable_routing_hint_emitted", {
                "turn_id": getattr(ctx, "_turn_id", None),
                "named_variables": list(verdict.named_variables)[:8],
            })
    return _compose_framed_question(
        _format_case_summary_block(case_summary),
        episodic_block, warmth_hint, verdict.redacted_question, variable_hint)
