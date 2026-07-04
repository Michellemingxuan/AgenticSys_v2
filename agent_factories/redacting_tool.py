"""Wraps an Agent as a tool with PII redaction on input + output boundaries."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

from agents import Agent, RunContextWrapper, Runner, function_tool
from agents.exceptions import AgentsException, MaxTurnsExceeded

from logger.process_timer import ProcessTimer
from llm.firewall_stack import LLM_CALL_KIND, redact_payload, sanitize_message
from tools.node_trace import _open_node, attach_extra, attach_tag
from tools.viz_renderer import kp_to_vega_spec, render_chart
from tools.series_extract import (
    _ParsedSeries,
    _parse_series_from_tool_outputs,
    _values_match,
    _fill_kp_numbers,
    _extract_data_tool_outputs,
)
from tools.distiller_pass import _distill_and_persist


# Inner-specialist turn budget. SDK default is 10. Lowered from 15 → 6
# after measuring real traces: data specialists were consistently using
# 2-3 rounds with the system_prompt batching guidance, and the rare
# 4th round was almost always over-exploration that didn't improve the
# answer. 6 gives a small safety margin for genuinely hard questions
# while shaving ~25-30s off the wall-clock outliers. Pair with the
# "emit final output ASAP" rule in data_query.md — together they
# discourage the model from looping past a clear answer.
_SPECIALIST_MAX_TURNS = 6

# Wall-clock budget per specialist call. Bounds hangs from stalled LLM /
# transport layers that ``max_turns`` alone can't catch. 240s is generous
# vs. the typical 20-90s specialist run, but well below the user-perceived
# "is this thing broken?" threshold so we surface the failure instead of
# letting the SSE stream stall.
_SPECIALIST_TIMEOUT_S = float(os.environ.get("SPECIALIST_TIMEOUT_S", "240"))

_SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES = 2
_ELIDED_SPECIALIST_TOOL_OUTPUT = (
    "(elided - earlier in-turn specialist tool output; rely on the latest "
    "turn context or re-query only if the value is still needed.)"
)


def _active_kps(kps: list[dict]) -> list[dict]:
    """Latest knowledge point per topic. The underlying list is appended to
    chronologically (never mutated), so iterating in order and keeping the
    last-seen entry per topic gives us the active set. Older entries with
    the same topic remain in the list for audit but are hidden from the
    digest the specialist sees on its next call.
    """
    active: dict[str, dict] = {}
    for kp in kps or []:
        topic = kp.get("topic")
        if topic:
            active[topic] = kp
    return list(active.values())


def _format_kb_digest(kps: list[dict], full_kb: dict | None = None,
                      self_name: str | None = None) -> str:
    """Render a short KB hint pointing to the lookup tools.

    Instead of dumping all KP claims into the input (which inflates token
    count on follow-up turns), we list topic names only and tell the
    specialist to use kb_lookup(topic) for details.

    When *full_kb* and *self_name* are provided, also lists topic counts
    from OTHER specialists so this specialist knows cross-domain data is
    available via kb_lookup / kb_list_topics without re-querying.
    """
    active = _active_kps(kps)
    parts: list[str] = []
    if active:
        topics = [kp.get("topic", "?") for kp in active]
        parts.append(
            f"[KB — your cached topics ({len(active)}): "
            f"{', '.join(topics)}.]"
        )
    other_lines: list[str] = []
    if full_kb and self_name:
        for spec, spec_kps in sorted(full_kb.items()):
            if spec == self_name:
                continue
            other_active = _active_kps(spec_kps)
            if other_active:
                other_topics = [kp.get("topic", "?") for kp in other_active]
                other_lines.append(f"  {spec}: {', '.join(other_topics)}")
    if other_lines:
        parts.append(
            "[KB — other specialists' cached topics "
            "(use kb_lookup(topic) to retrieve without re-querying):\n"
            + "\n".join(other_lines) + "]"
        )
    if not parts:
        return ""
    parts.append(
        "Call kb_lookup(topic) to get cached data before re-querying. "
        "Call kb_list_topics() to see all cached claims."
    )
    return "\n".join(parts)


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


async def _auto_chart_from_tool_outputs(
    app_ctx, name: str, tool_outputs: str,
) -> int:
    """Render charts from specialist tool outputs — no LLM needed.

    Parses summarize_trend / batch_summarize_trend / summarize_by_group
    results, determines the chart kind from the data shape, injects
    thresholds from the catalog, and renders. Runs as a fire-and-forget
    task in parallel with the distiller.
    """
    from tools.viz_renderer import kp_to_vega_spec, render_chart

    logger = getattr(app_ctx, "logger", None)
    kb = getattr(app_ctx, "_specialist_kb", None)
    case_folder = getattr(app_ctx, "case_folder", None)
    turn_id = getattr(app_ctx, "_turn_id", None)
    catalog = getattr(app_ctx, "_catalog", None)
    emit_event = getattr(app_ctx, "_emit_event", None)

    if kb is None or case_folder is None:
        if logger:
            logger.log("auto_chart_skipped", {
                "specialist": name, "reason": "no_kb_or_case_folder"})
        return 0

    try:
        parsed = _parse_series_from_tool_outputs(tool_outputs)
    except Exception as exc:
        if logger:
            logger.log("auto_chart_parse_failed", {
                "specialist": name, "error": str(exc)[:200]})
        return 0

    if not parsed:
        if logger:
            logger.log("auto_chart_skipped", {
                "specialist": name, "reason": "no_series_parsed",
                "tool_outputs_chars": len(tool_outputs)})
        return 0

    # Group series by key_field type (period vs group)
    trend_series = [ps for ps in parsed if ps.key_field == "period"]
    group_series = [ps for ps in parsed if ps.key_field in ("group", "merchant")]

    if logger:
        logger.log("auto_chart_series_parsed", {
            "specialist": name,
            "n_trend_series": len(trend_series),
            "n_group_series": len(group_series),
            "column_names": [ps.column_name for ps in parsed],
            "series_sizes": [len(ps.lookup) for ps in parsed],
        })

    charts_rendered = 0
    charts_dir = Path(case_folder) / "charts"

    try:
        charts_rendered = _render_auto_charts(
            trend_series, group_series, name, charts_dir,
            kb, turn_id, catalog, logger, emit_event,
        )
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.log("auto_chart_failed", {
                "specialist": name,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            })

    if charts_rendered and logger:
        logger.log("auto_chart_rendered", {
            "specialist": name,
            "n_charts": charts_rendered,
            "turn_id": turn_id,
        })
    return charts_rendered


def _render_auto_charts(
    trend_series, group_series, name, charts_dir,
    kb, turn_id, catalog, logger, emit_event=None,
) -> int:
    from tools.viz_renderer import kp_to_vega_spec, render_chart

    def _emit_pending(topic: str, kind: str) -> None:
        # Fire `chart_pending` BEFORE the render so the frontend shows a
        # "working on the plot…" placeholder during the gap until the
        # end-of-turn `chart` event lands. Keyed by (specialist, topic) to
        # match that eventual event so the placeholder clears cleanly. The
        # auto-chart task runs concurrently (right after the specialist
        # finishes, not at end-of-turn), so this fires early enough to be
        # useful. Mirrors make_chart's pending emit (tools/data_viz_tools.py).
        if callable(emit_event):
            try:
                emit_event("chart_pending", {
                    "specialist": name, "topic": topic, "kind": kind,
                })
            except Exception:  # noqa: BLE001 - emit must never break rendering
                pass

    charts_rendered = 0

    # ── Trend charts ──
    # Dedup: drop series that are identical (same column + same data).
    # This catches the case where the specialist calls summarize_trend
    # twice on the same table/column, producing two copies of the same
    # data that would otherwise render as a misleading dual-axis chart.
    deduped_trend: list[_ParsedSeries] = []
    seen_signatures: set[tuple] = set()
    for ps in trend_series:
        sig = (ps.column_name, ps.table_name,
               tuple(sorted(ps.lookup.items())))
        if sig not in seen_signatures:
            seen_signatures.add(sig)
            deduped_trend.append(ps)
    trend_series = deduped_trend

    # Disambiguate column names so chart legends are clear.
    # 1. Generic names ("Amount") get prefixed with table context
    #    when multiple trend series are present.
    # 2. Same column_name from different tables gets table-prefixed.
    # Preserve original column names before disambiguation (for
    # threshold lookup against catalog keys).
    orig_col_names: dict[int, str] = {
        id(ps): ps.column_name for ps in trend_series
    }
    _GENERIC_COLS = {"amount", "value", "balance", "total", "count"}
    _TABLE_LABEL = {"spends": "Spend", "payments": "Payment",
                    "spends_data": "Spend"}
    if len(trend_series) > 1:
        for ps in trend_series:
            if (ps.column_name
                    and ps.column_name.lower() in _GENERIC_COLS
                    and ps.table_name):
                prefix = _TABLE_LABEL.get(ps.table_name, ps.table_name)
                ps.column_name = f"{prefix} {ps.column_name}"
    col_counts: dict[str, int] = {}
    for ps in trend_series:
        c = ps.column_name or ""
        col_counts[c] = col_counts.get(c, 0) + 1
    for ps in trend_series:
        if ps.column_name and col_counts.get(ps.column_name, 0) > 1:
            if ps.table_name:
                ps.column_name = f"{ps.table_name} {ps.column_name}"

    # Build chart groups:
    #   1 series → single "trend" chart
    #   2 series, same unit + similar range → "trend" (shared y-axis)
    #   2 series, different units or ranges → "trend_dual" (dual y-axes)
    #   3+ series → individual "trend" charts (separate tabs in the UI)
    from tools.viz_renderer import _infer_unit

    def _series_range(ps: _ParsedSeries) -> tuple[float, float] | None:
        vals = []
        for v in ps.lookup.values():
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return (min(vals), max(vals)) if vals else None

    def _ranges_compatible(r0, r1) -> bool:
        """True when two value ranges are close enough to share one y-axis."""
        if r0 is None or r1 is None:
            return False
        all_max = max(r0[1], r1[1])
        all_min = min(r0[0], r1[0])
        span = all_max - all_min
        if span == 0:
            return True
        # Each series should cover at least 20% of the combined span,
        # otherwise the smaller one gets squashed flat.
        s0 = r0[1] - r0[0]
        s1 = r1[1] - r1[0]
        return min(s0, s1) / span >= 0.2

    if trend_series:
        if len(trend_series) == 2:
            u0 = _infer_unit(orig_col_names.get(id(trend_series[0]),
                             trend_series[0].column_name))
            u1 = _infer_unit(orig_col_names.get(id(trend_series[1]),
                             trend_series[1].column_name))
            same_unit = u0 and u1 and u0 == u1
            similar_range = _ranges_compatible(
                _series_range(trend_series[0]),
                _series_range(trend_series[1]),
            )
            if same_unit and similar_range:
                chart_groups = [("trend", trend_series)]
            else:
                chart_groups = [("trend_dual", trend_series)]
        elif len(trend_series) >= 3:
            chart_groups = [("trend", [ps]) for ps in trend_series]
        else:
            chart_groups = [("trend", trend_series)]

        for kind, group in chart_groups:
            y_fields = []
            for i, ps in enumerate(group):
                y_fields.append(ps.column_name or (
                    "value" if len(group) == 1 else f"series_{i}"))

            if len(group) == 1 and group[0].column_name:
                topic = f"{name}_{group[0].column_name}_trend"
            elif len(group) == 2:
                seen_cols: list[str] = []
                for ps in group:
                    if ps.column_name and ps.column_name not in seen_cols:
                        seen_cols.append(ps.column_name)
                cols = "_and_".join(seen_cols)
                topic = f"{cols}_trajectory"
            else:
                topic = f"{name}_trend"

            # Build points array
            all_periods: list = []
            seen: set = set()
            for ps in group:
                for k in ps.lookup:
                    if k not in seen:
                        all_periods.append(k)
                        seen.add(k)

            points = []
            for period in all_periods:
                row: dict = {"period": period}
                for i, ps in enumerate(group):
                    row[y_fields[i]] = ps.lookup.get(period)
                points.append(row)

            if len(points) < 4:
                continue

            # Inject thresholds from catalog. The y_field may have been
            # prefixed (e.g. "Spend Amount") so also try the original
            # column_name from the parsed series and catalog aliases.
            if catalog and hasattr(catalog, "get_thresholds"):
                # Build a map: y_field → original column_name for fallback
                yf_to_orig: dict[str, str] = {}
                for i, ps in enumerate(group):
                    yf_to_orig[y_fields[i]] = orig_col_names.get(
                        id(ps), ps.column_name or "")

                for cat_table in catalog.list_tables():
                    thresholds = catalog.get_thresholds(cat_table)
                    if not thresholds:
                        continue
                    aliases = catalog.column_aliases(cat_table)
                    for yf in y_fields:
                        th_key = (f"threshold_{yf}"
                                  if len(y_fields) > 1 else "threshold")
                        if any(p.get(th_key) is not None for p in points):
                            continue
                        # Try: exact match, original column_name, alias
                        match_val = None
                        for candidate in (yf, yf_to_orig.get(yf, "")):
                            if candidate in thresholds:
                                match_val = thresholds[candidate]["value"]
                                break
                            # Check if candidate is an alias
                            for canon, alias_list in aliases.items():
                                if (candidate in alias_list
                                        and canon in thresholds):
                                    match_val = thresholds[canon]["value"]
                                    break
                            if match_val is not None:
                                break
                        if match_val is not None:
                            for p in points:
                                p[th_key] = match_val

            # Build informative claim + source_call from data
            first_period = points[0]["period"] if points else "?"
            last_period = points[-1]["period"] if points else "?"
            claim_parts = []
            source_parts = []
            for yf in y_fields:
                vals = [p.get(yf) for p in points if p.get(yf) is not None]
                if vals:
                    first_v = vals[0]
                    last_v = vals[-1]
                    peak_v = max(vals)
                    trough_v = min(vals)
                    claim_parts.append(
                        f"{yf}: {first_v} → {last_v} "
                        f"(peak {peak_v}, trough {trough_v})"
                    )
                source_parts.append(
                    f"summarize_trend('{yf}', period='month')"
                )
            claim = (
                f"{first_period} to {last_period}: "
                + "; ".join(claim_parts)
            ) if claim_parts else f"{name} trend over {len(points)} periods"

            kp_dict = {
                "topic": topic,
                "claim": claim,
                "numbers": points,
                "viz": {"kind": kind, "x_field": "period", "y_fields": y_fields},
                "source_call": ", ".join(source_parts),
                "captured_at_turn": turn_id,
                "confidence": "high",
            }
            spec = kp_to_vega_spec(kp_dict)
            if spec:
                kp_dict["vega_spec"] = spec
            _emit_pending(topic, kind)
            img_path = render_chart(kp_dict, charts_dir, turn_id=turn_id, logger=logger)
            if img_path:
                kp_dict["image_path"] = img_path
                kb.setdefault(name, []).append(kp_dict)
                charts_rendered += 1

    # ── Group/bar charts ──
    for ps in group_series:
        if len(ps.lookup) < 4:
            continue

        def _to_num(v):
            if isinstance(v, (int, float)):
                return v
            try:
                s = str(v).replace(",", "").replace("$", "").strip()
                return float(s) if s else 0
            except (ValueError, TypeError):
                return 0

        points = [
            {ps.key_field: k, "value": _to_num(v)}
            for k, v in ps.lookup.items()
        ]
        topic_slug = ps.column_name or f"{name}_breakdown"
        # Build informative claim from data
        sorted_items = sorted(points, key=lambda x: x["value"], reverse=True)
        top_item = sorted_items[0] if sorted_items else {}
        top_label = top_item.get(ps.key_field, "?")
        top_val = top_item.get("value", 0)
        total = sum(p["value"] for p in points)
        top_share = (top_val / total * 100) if total else 0
        bar_claim = (
            f"Top {ps.key_field}: {top_label} ({top_val:,.0f}, "
            f"{top_share:.0f}% of total) across {len(points)} groups"
        )
        # Use horizontal bars (share) for readable long category names
        kp_dict = {
            "topic": topic_slug,
            "claim": bar_claim,
            "numbers": points,
            "viz": {"kind": "share", "x_field": ps.key_field, "y_fields": ["value"]},
            "source_call": f"summarize_by_group('{ps.table_name}', '{ps.key_field}', '{ps.column_name}', op='sum')",
            "captured_at_turn": turn_id,
            "confidence": "high",
        }
        spec = kp_to_vega_spec(kp_dict)
        if spec:
            kp_dict["vega_spec"] = spec
        _emit_pending(topic_slug, "share")
        img_path = render_chart(kp_dict, charts_dir, turn_id=turn_id, logger=logger)
        if img_path:
            kp_dict["image_path"] = img_path
            kb.setdefault(name, []).append(kp_dict)
            charts_rendered += 1

    return charts_rendered


def _record_failure(app_ctx, name: str, sub_question: str,
                    error_type: str, message: str, exc: BaseException | None) -> str:
    """Log + persist a specialist failure, return the structured payload the
    orchestrator sees in place of the SpecialistOutput JSON.

    Two consumers read what we record here:
      • The orchestrator LLM gets the returned string and can decide whether
        to fall back (call a different specialist, mark a data_gap, narrow
        the sub-question, etc.). The ``[FAILED ...]`` sentinel lets it
        recognize the response as a failure and not as content to synthesize.
      • The server stream loop drains ``app_ctx._specialist_errors`` to emit
        typed ``error`` SSE events and to append flags to the FinalAnswer,
        so the reviewer sees the actual cause instead of a silent drop.
    """
    logger = getattr(app_ctx, "logger", None)
    if logger is not None:
        logger.log("specialist_call_failed", {
            "specialist": name,
            "error_type": error_type,
            "error_message": message,
            "sub_question": sub_question[:500],
            # Truncated traceback only — full one is reproducible from the
            # error_type + message and would bloat the JSONL.
            "traceback_tail": (traceback.format_exc().splitlines()[-1]
                               if exc is not None else ""),
        })
    errors = getattr(app_ctx, "_specialist_errors", None)
    if isinstance(errors, list):
        errors.append({
            "specialist": name,
            "error_type": error_type,
            "error_message": message,
            "sub_question": sub_question,
        })
    return (
        f"[FAILED {name}] {error_type}: {message}\n"
        f"This specialist could not produce a SpecialistOutput for this "
        f"sub-question. Treat as a data_gap for this domain — proceed with "
        f"other specialists' findings and note the failure in your flags. "
        f"If retry is appropriate, narrow the sub-question (e.g., limit to "
        f"a single metric or period)."
    )


def _normalize_subq(text: str) -> str:
    """Collapse whitespace + lowercase a sub-question for the per-AppContext
    dedup cache. Two sub-questions with trivial wording differences ('Did
    the customer have any returns?' vs 'did the customer have any returns')
    map to the same key.
    """
    return " ".join((text or "").strip().lower().split())


def redacting_tool(
    agent: Agent,
    name: str,
    description: str,
    *,
    timeout_s: float = _SPECIALIST_TIMEOUT_S,
    max_turns: int = _SPECIALIST_MAX_TURNS,
):
    """Return a FunctionTool that runs ``agent`` with input/output redaction.

    ``timeout_s`` / ``max_turns`` override the per-specialist budgets. They
    default to the domain-specialist values (240s / 6 turns); auxiliary agents
    like ``report_agent`` (a shallow file lookup, not analysis) should pass a
    much tighter budget so a stalled LLM round fails fast and best-effort
    instead of dragging the whole turn toward the 240s fence.

    Inter-agent transit boundary: anything flowing in (LLM-generated sub-
    question) gets ``sanitize_message``; anything flowing out (the inner
    agent's final output) gets ``redact_payload``.

    Multi-turn behavior: when ``ctx.context`` carries a
    ``_specialist_histories`` dict (see ``AppContext``), this wrapper reads
    the entry keyed by ``name`` to find the specialist's prior conversation
    and prepends it to the new sub-question on each call. After the run,
    the updated history (``result.to_input_list()``) is saved back. So a
    follow-up tool call to the same specialist within the same AppContext
    sees what the specialist already asked / answered, instead of starting
    fresh. Reset by constructing a new AppContext.
    """
    inner = agent

    @function_tool(name_override=name, description_override=description)
    async def _runner(ctx: RunContextWrapper, sub_question: str) -> str:
        runner_started = time.perf_counter()
        redacted_in = sanitize_message(sub_question)

        # Look up per-specialist history on the surrounding AppContext.
        # When the context doesn't expose `_specialist_histories` (e.g.
        # tests with a bare context object), behave like the legacy
        # single-turn path.
        app_ctx = ctx.context if ctx else None
        logger = getattr(app_ctx, "logger", None)
        timer = ProcessTimer(
            logger,
            "specialist_call",
            turn_id=getattr(app_ctx, "_turn_id", None),
            specialist=name,
        )
        histories = getattr(app_ctx, "_specialist_histories", None)
        prior = histories.get(name) if isinstance(histories, dict) else None

        # Per-AppContext dedup: same (specialist, sub_question) within the
        # same context returns the cached payload rather than re-running.
        # This caps cost when the orchestrator (especially in safechain mode,
        # where parallel-tool-call semantics aren't native) emits the same
        # call multiple times in one turn with trivial wording variations.
        cache_key = (name, _normalize_subq(redacted_in))
        seen = getattr(app_ctx, "_specialist_call_cache", None)
        if seen is None and app_ctx is not None:
            try:
                seen = {}
                # Attach lazily so each AppContext gets its own cache; tests
                # with a bare SimpleNamespace tolerate the attr add.
                app_ctx._specialist_call_cache = seen  # type: ignore[attr-defined]
            except Exception:
                seen = None
        if isinstance(seen, dict) and cache_key in seen:
            cached = seen[cache_key]
            if logger is not None:
                logger.log("specialist_call_dedup_hit",
                           {"specialist": name,
                            "sub_question_norm": cache_key[1]})
            # Tag the active (parent / orchestrator) node so optimization
            # reports can surface dedup hit-rate without re-deriving it.
            attach_tag("specialist_dedup_hit")
            timer.summary(
                outcome="dedup_hit",
                total_ms=int((time.perf_counter() - runner_started) * 1000),
                sub_question_chars=len(redacted_in),
            )
            return cached

        # Programmatic HARD GATE: block general_specialist when < 2 domain
        # specialists ran this turn. The orchestrator prompt says this but
        # the model sometimes ignores it; this enforces it server-side.
        _NON_DOMAIN = {"report_agent", "general_specialist"}
        domain_called = getattr(app_ctx, "_domain_specialists_called", None)
        if name not in _NON_DOMAIN:
            if isinstance(domain_called, set):
                domain_called.add(name)
        elif name == "general_specialist":
            n_domain = len(domain_called) if isinstance(domain_called, set) else 0
            if n_domain < 2:
                if logger is not None:
                    logger.log("general_specialist_blocked", {
                        "reason": "fewer_than_2_domain_specialists",
                        "domain_specialists_called": sorted(domain_called)
                        if isinstance(domain_called, set) else [],
                    })
                timer.summary(
                    outcome="blocked",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return (
                    "[SKIPPED — only 1 domain specialist called. "
                    "Emit FinalAnswer NOW from the specialist + report_agent outputs.]"
                )

        # KB digest preface — the specialist's accumulated knowledge from
        # earlier turns. Only prepend on the FIRST call within this turn (no
        # intra-turn `prior` exists yet); on subsequent within-turn calls the
        # `prior` transcript already carries the digest from the first call's
        # input message, so re-prepending would duplicate it.
        contextual_in = redacted_in
        kb_digest_n_kps = 0
        if not prior:
            kb_obj = getattr(app_ctx, "_specialist_kb", None)
            if isinstance(kb_obj, dict):
                kps_for_name = kb_obj.get(name, [])
                kb_digest = _format_kb_digest(
                    kps_for_name, full_kb=kb_obj, self_name=name,
                )
                if kb_digest:
                    contextual_in = (
                        f"{kb_digest}\n\n--- New question ---\n{redacted_in}"
                    )
                    kb_digest_n_kps = len(_active_kps(kps_for_name))

        # Inject the case folder file list for report_agent so it can
        # use the report_needle skill's concept→file routing table to
        # pick relevant files by topic. The model calls fs_read_file on
        # 1-2 relevant files (batched) rather than reading everything.
        if name == "report_agent" and not prior:
            case_folder = getattr(app_ctx, "case_folder", None)
            if (case_folder is not None
                    and hasattr(case_folder, "exists") and case_folder.exists()):
                files = sorted(
                    p.name for p in case_folder.iterdir()
                    if p.is_file() and p.suffix in (".md", ".txt", ".csv")
                )
                if files:
                    file_list = ", ".join(files)
                    contextual_in = (
                        f"[Report files in case folder: {file_list}. "
                        f"To read a file, call the fs_read_file tool with "
                        f'filename="<name>".]\n\n'
                        f"{contextual_in}"
                    )

        if prior:
            run_input = prior + [{"role": "user", "content": contextual_in}]
        else:
            run_input = contextual_in
        timer.record(
            "specialist_context_prepare",
            int((time.perf_counter() - runner_started) * 1000),
            has_prior=bool(prior),
            kb_digest_prepended=contextual_in != redacted_in,
            sub_question_chars=len(redacted_in),
            run_input_items=len(run_input) if isinstance(run_input, list) else 1,
        )

        # Specialist run with retry on ModelBehaviorError. SafeChain can
        # truncate long JSON outputs, causing parse failures on the first
        # attempt. A retry often succeeds because the model produces a
        # shorter response. Max 2 attempts (1 initial + 1 retry).
        _MAX_SPECIALIST_ATTEMPTS = 2
        result = None
        last_exc = None
        node_store = getattr(app_ctx, "_node_trace_store", None)
        node_label = name if name == "report_agent" else f"specialist.{name}"

        for _attempt in range(_MAX_SPECIALIST_ATTEMPTS):
            try:
                t0 = time.perf_counter()
                kind_token = LLM_CALL_KIND.set("specialist")
                try:
                    label = node_label if _attempt == 0 else f"{node_label}.retry"
                    async with _open_node(node_store, label, depth=0):
                        if _attempt == 0:
                            if kb_digest_n_kps:
                                attach_tag("kb_digest_present")
                                attach_extra(n_kps_in_digest=kb_digest_n_kps)
                            if prior:
                                attach_tag("warm_specialist")
                        else:
                            attach_tag("retry")
                        result = await asyncio.wait_for(
                            Runner.run(
                                inner, run_input, context=app_ctx,
                                max_turns=max_turns,
                            ),
                            timeout=timeout_s,
                        )
                finally:
                    LLM_CALL_KIND.reset(kind_token)
                timer.record(
                    "specialist_runner",
                    int((time.perf_counter() - t0) * 1000),
                    max_turns=max_turns,
                    attempt=_attempt,
                )
                break  # success
            except MaxTurnsExceeded as exc:
                timer.summary(
                    outcome="failed",
                    error_type="max_turns_exceeded",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                    "max_turns_exceeded",
                    f"hit the {max_turns}-turn budget — "
                    f"partial findings were not returned. {exc}",
                    exc,
                )
            except asyncio.TimeoutError as exc:
                timer.summary(
                    outcome="failed",
                    error_type="timeout",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                    "timeout",
                    f"specialist did not complete within "
                    f"{timeout_s:.0f}s wall-clock budget.",
                    exc,
                )
            except asyncio.CancelledError:
                timer.summary(
                    outcome="cancelled",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                raise  # let the turn-level cancellation propagate
            except AgentsException as exc:
                last_exc = exc
                if _attempt + 1 < _MAX_SPECIALIST_ATTEMPTS:
                    if logger is not None:
                        logger.log("specialist_retry", {
                            "specialist": name,
                            "attempt": _attempt,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:200],
                        })
                    continue  # retry
                timer.summary(
                    outcome="failed",
                    error_type=type(exc).__name__,
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                    type(exc).__name__,
                    str(exc) or "no message",
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 - last-resort fence
                timer.summary(
                    outcome="failed",
                    error_type=type(exc).__name__,
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                type(exc).__name__,
                str(exc) or repr(exc),
                exc,
            )

        # Persist the updated history so the next call to this specialist
        # in the same context picks up where we left off.
        if isinstance(histories, dict) and hasattr(result, "to_input_list"):
            t0 = time.perf_counter()
            next_history = result.to_input_list()
            next_history, history_stats = _compact_specialist_history(next_history)
            histories[name] = next_history
            timer.record(
                "specialist_history_compact",
                int((time.perf_counter() - t0) * 1000),
                **history_stats,
            )
            if history_stats["items_elided"]:
                if logger is not None:
                    logger.log("specialist_history_compacted", {
                        "specialist": name,
                        **history_stats,
                        "kept_recent_user_messages":
                            _SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES,
                    })

        # Guard: Runner.run can complete without exception yet leave
        # final_output=None when the SDK's output-type parser silently
        # fails (observed on the SafeChain path where truncated JSON
        # repairs into valid-but-schemeless content). Without this check
        # the tool returns None → UI shows "no result" and the
        # orchestrator synthesizes from incomplete specialist data.
        if result is None or getattr(result, "final_output", None) is None:
            timer.summary(
                outcome="failed",
                error_type="empty_final_output",
                total_ms=int((time.perf_counter() - runner_started) * 1000),
            )
            return _record_failure(
                app_ctx, name, redacted_in,
                "empty_final_output",
                "Runner.run completed but final_output is None — "
                "the model likely produced content that did not match "
                "the SpecialistOutput schema.",
                last_exc,
            )

        t0 = time.perf_counter()
        try:
            payload = redact_payload(result.final_output)
        except Exception as exc:  # noqa: BLE001
            # Output redaction failure is rare but should not look like a
            # silent drop. Surface it the same way as a run failure.
            timer.summary(
                outcome="failed",
                error_type=f"redact_{type(exc).__name__}",
                total_ms=int((time.perf_counter() - runner_started) * 1000),
            )
            return _record_failure(
                app_ctx, name, redacted_in,
                f"redact_{type(exc).__name__}",
                f"output redaction failed: {exc}",
                exc,
            )
        # Inject the sub-question into the payload so the orchestrator
        # (and general_specialist reading the outputs) knows what each
        # specialist was answering — without the specialist wasting output
        # tokens to echo it. Replaces the removed SpecialistOutput.question
        # field with a zero-cost server-side injection.
        if isinstance(payload, str) and name != "report_agent":
            payload = f"[Sub-question: {redacted_in}]\n{payload}"

        timer.record(
            "specialist_output_redact",
            int((time.perf_counter() - t0) * 1000),
            payload_chars=len(payload) if isinstance(payload, str) else 0,
        )

        # Second pass — distill knowledge points from the (un-redacted)
        # SpecialistOutput. We FIRE AND FORGET so the orchestrator receives
        # the specialist's payload immediately (no distiller round-trip on
        # the critical path). Server.py awaits all pending distillers at
        # end-of-turn so the KB is fully populated before the next turn's
        # warmth digest is built.
        pending = getattr(app_ctx, "_pending_distillers", None)
        t0 = time.perf_counter()
        try:
            tool_outputs = _extract_data_tool_outputs(result)
            if logger is not None and name != "report_agent":
                logger.log("distiller_tool_outputs_extracted", {
                    "specialist": name,
                    "tool_outputs_chars": len(tool_outputs),
                    "n_items": len(result.to_input_list()) if hasattr(result, "to_input_list") else -1,
                })
            # Fire TWO parallel async tasks:
            # 1. Distiller: extract claims into KB for follow-ups
            # 2. Auto-chart: render charts from tool outputs (no LLM needed)
            task = asyncio.create_task(
                _distill_and_persist(
                    app_ctx, name, redacted_in, result.final_output,
                    tool_outputs=tool_outputs,
                ),
                name=f"distill-{name}",
            )
            if isinstance(pending, list):
                pending.append(task)
            # Auto-chart: parse tool outputs for series data, render charts
            if name != "report_agent" and tool_outputs:
                chart_task = asyncio.create_task(
                    _auto_chart_from_tool_outputs(
                        app_ctx, name, tool_outputs,
                    ),
                    name=f"autochart-{name}",
                )
                if isinstance(pending, list):
                    pending.append(chart_task)
        except Exception as exc:  # noqa: BLE001 - belt-and-suspenders
            if logger is not None:
                logger.log("distiller_outer_failure", {
                    "specialist": name,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                })
        timer.record(
            "distiller_schedule",
            int((time.perf_counter() - t0) * 1000),
            pending_distillers=len(pending) if isinstance(pending, list) else None,
        )

        if isinstance(seen, dict):
            seen[cache_key] = payload
        timer.summary(
            outcome="ok",
            total_ms=int((time.perf_counter() - runner_started) * 1000),
            sub_question_chars=len(redacted_in),
        )
        return payload

    return _runner
