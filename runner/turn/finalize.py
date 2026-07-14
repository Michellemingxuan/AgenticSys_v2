"""Turn finalization: chart collection + fallback/salvage answer synthesis."""
from __future__ import annotations

import json
from pathlib import Path

from tools.viz_renderer import kp_to_vega_spec


def _synthesize_fallback_answer(
    tool_calls: list[dict],
    error_kind: str,
    error_message: str,
) -> tuple[str, list[str]]:
    """Build a best-effort answer from the specialist outputs we have when the
    orchestrator's final synthesis fails (e.g. ModelBehaviorError on FinalAnswer
    parsing — the model emitted truncated/malformed JSON).

    Without this fallback, every specialist run is wasted because the SDK
    raises before ``streamed.final_output`` is populated. We salvage the
    individual SpecialistOutput payloads we already streamed and present them
    as a bulleted "what each specialist found" block so the reviewer at least
    sees the underlying findings.

    Returns ``(answer_markdown, flags)``. The flags carry the structured
    failure cause so it lands in the FinalAnswer audit trail too.
    """
    _AUX_TOOLS = {"report_agent", "general_specialist"}

    def _excerpt(payload) -> str:
        """Pull the most-readable field from a specialist payload, capped."""
        if payload is None:
            return "(no payload)"
        # Specialists typically return SpecialistOutput {answer, findings,
        # data_gap, ...}. After redact_payload + _safe_dump it's a dict; on
        # failure paths it's a "[FAILED …]" string.
        if isinstance(payload, str):
            return payload[:600]
        if isinstance(payload, dict):
            for key in ("answer", "findings", "summary", "data_gap"):
                v = payload.get(key)
                if v:
                    return (str(v) if not isinstance(v, str) else v)[:600]
            # No known field — dump compactly.
            try:
                return json.dumps(payload, default=str)[:600]
            except Exception:
                return str(payload)[:600]
        return str(payload)[:600]

    successful = [c for c in tool_calls if "payload" in c]
    domain_results = [c for c in successful if c["tool"] not in _AUX_TOOLS]
    aux_results = [c for c in successful if c["tool"] in _AUX_TOOLS]

    lines = [
        "**The agent could not produce a synthesized answer for this turn.** "
        "The orchestrator's final-output step failed before it could combine "
        "the specialists' findings. Below is what each specialist returned "
        "this run — review them directly.",
        "",
    ]
    if domain_results:
        lines.append("**Specialist findings**")
        for c in domain_results:
            lines.append(f"- **{c['tool']}** — {_excerpt(c.get('payload'))}")
        lines.append("")
    if aux_results:
        lines.append("**Reports / cross-domain review**")
        for c in aux_results:
            lines.append(f"- **{c['tool']}** — {_excerpt(c.get('payload'))}")
        lines.append("")
    if not successful:
        lines.append(
            "_No specialists produced a result before the orchestrator failed._"
        )

    lines.extend([
        "---",
        f"_Error category: `{error_kind}`. Re-ask the question (often a "
        f"transient model-output issue) or narrow the scope — e.g. ask about "
        f"one domain at a time._",
    ])

    flags = [
        f"orchestrator_failed: {error_kind}",
        f"fallback_answer: synthesized from {len(domain_results)} specialist(s)",
    ]
    return "\n".join(lines), flags


def _replay_completed_specialists(sess, turn_id: str, tool_calls: list[dict]) -> None:
    """Re-emit ``team_plan`` + ``agent_completed`` for specialists that produced
    a payload this turn.

    Error / fallback branches that emit ``final`` MUST call this first, or the
    reasoning-trace panel loses the work that DID succeed: a turn-wide ``error``
    leaves completed specialists rendered as "failed" with no trace (see the
    ``alternate_paths_must_replay_full_sse`` project rule). ``agent_completed``
    carries the payload and the frontend upserts by ``call_id``, so re-emitting
    is idempotent on the success path and restorative on the failure path.
    """
    completed = [c for c in tool_calls if "payload" in c]
    if not completed:
        return
    sess.emit("team_plan", {"turn_id": turn_id, "tool_calls": list(tool_calls)})
    for c in completed:
        sess.emit("agent_completed", {
            "turn_id": turn_id,
            "call_id": c.get("call_id"),
            "tool": c.get("tool"),
            "payload": c.get("payload"),
            "duration_ms": c.get("duration_ms", 0),
        })


def _chart_signature(kp: dict) -> tuple | None:
    """A content fingerprint for a chart — independent of which specialist made
    it, how the y-column is named, or injected thresholds. Two specialists that
    trend the SAME table/column over the SAME window produce the SAME figure;
    this lets the turn show it once. Signature = ``(kind, {(x_value, y_value)})``
    over the numeric cells, floats rounded to swallow repr noise. Returns
    ``None`` when there's nothing fingerprintable (e.g. no numbers)."""
    viz = kp.get("viz") if isinstance(kp, dict) else None
    viz = viz if isinstance(viz, dict) else {}
    kind = viz.get("kind")
    numbers = kp.get("numbers")
    if not kind or not isinstance(numbers, list) or not numbers:
        return None
    x = viz.get("x_field") or ""
    if not x:
        return None
    pairs: set = set()
    y_values: set = set()
    x_seen = False
    for row in numbers:
        if not isinstance(row, dict) or x not in row:
            continue
        x_seen = True
        xv = str(row.get(x))
        for k, v in row.items():
            if k == x or (isinstance(k, str) and k.startswith("threshold")):
                continue
            try:
                yv = round(float(v), 6)
            except (TypeError, ValueError):
                continue
            pairs.add((xv, yv))
            y_values.add(yv)
    # Only fingerprint when the match is UNAMBIGUOUS: a real x-axis present in
    # the rows AND a non-flat series (>= 2 distinct y-values). A flat/constant
    # series (many sparse features are all-zero) or an x-less chart yields a
    # signature too weak to tell two DISTINCT metrics apart — so we return None
    # and never risk deduping those away. Erring toward keeping charts.
    if not x_seen or len(y_values) < 2:
        return None
    return (kind, frozenset(pairs))


def _collect_turn_charts(specialist_kb: dict, turn_id: str, case_id: str) -> list[dict]:
    """Find every KP captured in this turn that surfaces in the Plots panel.

    Two kinds of KP qualify:
      1. Rendered charts — KPs with an ``image_path`` set by
         ``render_chart``. Returned with ``url`` pointing at the Flask
         route ``/api/cases/<case_id>/charts/<filename>``.
      2. Table KPs — ``viz.kind == "table"`` (no image; the rows are
         shown as an HTML table in the panel). Returned with empty
         ``url`` and ``numbers`` carrying the row data.

    Deduped by ``(specialist, topic)`` — when both the `make_chart` tool
    and the auto-distiller produce an entry for the same topic in one
    turn, the latest one wins (chronological iteration order) — AND by
    content fingerprint across specialists, so two specialists plotting the
    SAME figure (e.g. modeling and spend_payments both charting the monthly
    spend trend) surface it only once. First occurrence wins.
    """
    if not isinstance(specialist_kb, dict):
        return []
    by_key: dict[tuple[str, str], dict] = {}
    kp_by_key: dict[tuple[str, str], dict] = {}
    for spec_name, kps in specialist_kb.items():
        if not isinstance(kps, list):
            continue
        for kp in kps:
            if not isinstance(kp, dict):
                continue
            if kp.get("captured_at_turn") != turn_id:
                continue
            img_path = kp.get("image_path")
            viz = kp.get("viz") or {}
            kind = viz.get("kind", "") if isinstance(viz, dict) else ""
            is_table = kind == "table"
            if not img_path and not is_table:
                continue
            topic = kp.get("topic", "chart")
            entry: dict = {
                "topic": topic,
                "specialist": spec_name,
                "url": "",
            }
            if img_path:
                entry["url"] = f"/api/cases/{case_id}/charts/{Path(img_path).name}"
            # Latest wins per (specialist, topic). Iteration order over
            # the KB's chronological list means the last appended entry
            # naturally overwrites the earlier one for the same key.
            by_key[(spec_name, topic)] = entry
            kp_by_key[(spec_name, topic)] = kp

    # Cross-specialist content dedup: drop a later chart whose data fingerprint
    # matches one already kept (same figure from a different specialist). Charts
    # with no fingerprint (empty/non-numeric) are never deduped away.
    seen: set = set()
    deduped: list[dict] = []
    for key, entry in by_key.items():
        sig = _chart_signature(kp_by_key[key])
        if sig is not None:
            if sig in seen:
                continue
            seen.add(sig)
        deduped.append(entry)
    return deduped


def _build_chart_payload(kp: dict | None, chart: dict) -> dict:
    """Build the ``chart`` SSE payload from a KP + its collected chart meta.

    Single source of truth for the payload the frontend receives — both the
    live end-of-turn emit and the QA-cache replay flow through here (the
    payload is what gets stored in the cache and re-emitted verbatim).

    The Vega-Lite spec is REGENERATED here via ``kp_to_vega_spec(kp)`` rather
    than read off the KP. We deliberately do NOT persist ``vega_spec`` on the
    KP: it roughly duplicates ``numbers`` plus a lot of Vega scaffolding, and
    the KP flows into ``specialist_kb`` → distilled memory → cross-turn
    context, so storing it there bloats everything downstream. Regeneration
    is cheap and keeps the interactive-chart contract intact. Returns ``None``
    for the spec on non-viz-able KPs (e.g. ``table`` kind, empty numbers),
    exactly as before.
    """
    viz = (kp or {}).get("viz") or {}
    kind = viz.get("kind", "") if isinstance(viz, dict) else ""
    payload: dict = {
        "specialist": chart["specialist"],
        "topic": chart["topic"],
        "url": chart["url"],
        "claim": (kp or {}).get("claim", ""),
        "source_call": (kp or {}).get("source_call", ""),
        "kind": kind,
        "vega_spec": kp_to_vega_spec(kp) if kp else None,
    }
    if kind == "table":
        payload["numbers"] = (kp or {}).get("numbers") or []
        payload["x_field"] = viz.get("x_field", "") if isinstance(viz, dict) else ""
        payload["y_fields"] = viz.get("y_fields") or [] if isinstance(viz, dict) else []
    return payload
