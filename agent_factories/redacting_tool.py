"""Wraps an Agent as a tool with PII redaction on input + output boundaries."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
import traceback
from pathlib import Path

from agents import Agent, RunContextWrapper, Runner, function_tool
from agents.exceptions import AgentsException, MaxTurnsExceeded

from logger.process_timer import ProcessTimer
from llm.firewall_stack import LLM_CALL_KIND, redact_payload, sanitize_message
from tools.node_trace import _open_node, attach_extra, attach_tag
from tools.viz_renderer import kp_to_vega_spec, render_chart


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
_SPECIALIST_TIMEOUT_S = 240.0

# Wall-clock budget for the second-pass distiller. Distillation is purely
# text-extraction; should be fast. If it stalls, log + skip — the specialist
# answer is already in flight to the orchestrator and we degrade gracefully
# to "no KB update this turn."
#
# Bumped 30s → 60s after observing real-world timeouts on chunky
# specialists (spend_payments returning ~8 chartable claims at once,
# case 366132845011 turn around 06:20). The distiller timing out kills
# BOTH KB warmth for the next turn AND charts for the current turn (the
# auto-distiller is the primary chart-generation path; make_chart is
# specialist-explicit and proves unreliable when the LLM forgets). 60s
# is still under the slowest specialist budget (240s) so end-of-turn
# drain doesn't blow up.
_DISTILLER_TIMEOUT_S = 60.0

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


def _format_kb_digest(kps: list[dict]) -> str:
    """Render a short KB hint pointing to the lookup tools.

    Instead of dumping all KP claims into the input (which inflates token
    count on follow-up turns), we list topic names only and tell the
    specialist to use kb_lookup(topic) for details.
    """
    active = _active_kps(kps)
    if not active:
        return ""
    topics = [kp.get("topic", "?") for kp in active]
    return (
        f"[KB: {len(active)} cached topics from earlier turns: "
        f"{', '.join(topics)}. "
        f"Call kb_lookup(topic) to get cached data before re-querying. "
        f"Call kb_list_topics() to see all cached claims.]"
    )


# Keywords that signal series/trend/concentration data — the kind the
# distiller actually extracts into chartable KPs. These come from tool
# outputs (summarize_trend summary block, summarize_by_group concentration
# block) and are reliable markers that the output is worth distilling.
# Without these, the specialist used scalar tools (aggregate_column) or
# query_table row dumps — in either case the distiller consistently
# returns empty knowledge_points and wastes 10-20s.
_SERIES_KEYWORDS = frozenset({
    "slope", "peak", "trough", "pct_change", "coefficient",
    "hhi", "top1_share", "top3_share", "concentration",
    "trend", "trajectory", "missing_periods",
})


def _is_narrow_output(specialist_output, sub_question: str = "") -> bool:
    """Detect narrow specialist outputs (simple counts, yes/no) where
    distillation costs 10-20s LLM round-trip but yields 0 knowledge points.

    Two-gate check:
    1. Series keywords in findings+evidence → always distill (the
       distiller extracts chartable KPs from these)
    2. No series keywords + short findings → narrow, skip distiller
    """
    if not hasattr(specialist_output, "findings"):
        return False
    findings = getattr(specialist_output, "findings", "") or ""
    evidence = getattr(specialist_output, "evidence", None) or []

    all_text = (findings + " " + " ".join(
        e for e in evidence if isinstance(e, str)
    )).lower()

    if any(kw in all_text for kw in _SERIES_KEYWORDS):
        return False

    return len(findings) <= 150


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


@dataclasses.dataclass
class _ParsedSeries:
    """A parsed data series with optional column-name metadata."""
    lookup: dict  # {period_or_group: value}
    column_name: str  # from batch_summarize_trend's value_column, or ""
    key_field: str  # "period" or "group" or "merchant"


def _parse_series_from_tool_outputs(tool_outputs_text: str) -> list[_ParsedSeries]:
    """Parse JSON tool outputs and extract data series with metadata.

    Returns a list of _ParsedSeries, each with:
    - lookup: {period_or_group: value} for fast point access
    - column_name: the value_column from batch_summarize_trend (for
      matching to y_fields), or "" if unknown
    - key_field: "period" or "group"
    """
    results: list[_ParsedSeries] = []
    if not tool_outputs_text:
        return results

    def _extract_json_objects(text: str):
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(text):
            pos = text.find("{", idx)
            if pos == -1:
                break
            try:
                obj, end = decoder.raw_decode(text, pos)
                if isinstance(obj, dict):
                    yield obj
                idx = end
            except json.JSONDecodeError:
                idx = pos + 1

    def _series_to_parsed(series: list[dict], column_name: str = "") -> _ParsedSeries | None:
        if not series or not isinstance(series[0], dict):
            return None
        key_field = None
        for candidate in ("period", "group", "merchant"):
            if series[0].get(candidate) is not None:
                key_field = candidate
                break
        if not key_field:
            return None
        lookup = {}
        for row in series:
            k = row.get(key_field)
            # Prefer raw_value (numeric) over value (formatted string)
            v = row.get("raw_value") or row.get("value")
            if k is not None and v is not None:
                lookup[k] = v
        return _ParsedSeries(lookup=lookup, column_name=column_name,
                             key_field=key_field) if lookup else None

    for parsed in _extract_json_objects(tool_outputs_text):
        # summarize_trend: {series: [...], summary: {...}}
        s = parsed.get("series")
        if isinstance(s, list) and len(s) >= 2:
            ps = _series_to_parsed(s)
            if ps:
                results.append(ps)

        # batch_summarize_trend: {results: [{value_column, result: "<json>"}, ...]}
        # Note: each result's "result" field is a JSON STRING (from
        # _summarize_trend_impl), not a nested dict. Must parse it.
        batch = parsed.get("results")
        if isinstance(batch, list):
            for r in batch:
                if not isinstance(r, dict):
                    continue
                col = r.get("value_column", "")
                # Try direct "series" key first (future-proofing)
                s = r.get("series")
                # If not found, parse the "result" string
                if s is None and isinstance(r.get("result"), str):
                    try:
                        inner = json.loads(r["result"])
                        s = inner.get("series") if isinstance(inner, dict) else None
                    except (json.JSONDecodeError, ValueError):
                        pass
                if isinstance(s, list) and len(s) >= 2:
                    ps = _series_to_parsed(s, column_name=str(col))
                    if ps:
                        results.append(ps)

        # summarize_by_group: {groups: [...], concentration: {...}}
        g = parsed.get("groups")
        if isinstance(g, list) and len(g) >= 2:
            ps = _series_to_parsed(g)
            if ps:
                results.append(ps)

    return results


def _values_match(a: float, b: float) -> bool:
    """Compare two numeric values with relative + absolute tolerance."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom < 0.001  # 0.1% relative tolerance


def _fill_kp_numbers(kp_dict: dict, parsed_series: list[_ParsedSeries]) -> None:
    """Fill or construct KP numbers from parsed tool output series.

    Three modes:
    1. **numbers is empty** + viz has y_fields → construct full array.
    2. **numbers has rows with nulls** → fill from matched series.
    3. **numbers is complete** → no-op.

    Matching priority for assigning a parsed series to a y_field:
    1. column_name match (from batch_summarize_trend's value_column)
    2. Anchor-value match (compare known non-null values at same periods)
    3. Order fallback (assign remaining unmatched series in order)
    """
    numbers = kp_dict.get("numbers")
    viz = kp_dict.get("viz") or {}
    y_fields = viz.get("y_fields") or []

    if not parsed_series:
        return

    # Determine the key field
    key_field = None
    source = numbers[0] if numbers and isinstance(numbers[0], dict) else None
    if source is None and parsed_series:
        key_field = parsed_series[0].key_field
    else:
        for candidate in ("period", "group", "merchant"):
            if source and source.get(candidate) is not None:
                key_field = candidate
                break
    if not key_field:
        return

    # Filter to series with matching key_field
    matching = [ps for ps in parsed_series if ps.key_field == key_field]
    if not matching:
        return

    def _match_series_to_fields(
        fields: list[str],
        series: list[_ParsedSeries],
        anchor_data: dict[str, dict] | None = None,
    ) -> dict[str, _ParsedSeries]:
        """Assign each y_field to a parsed series. Priority:
        1. column_name exact match
        2. Anchor-value match (if anchor_data provided)
        3. Order fallback
        """
        result: dict[str, _ParsedSeries] = {}
        used: set[int] = set()

        # Pass 1: column_name match
        for f in fields:
            for i, ps in enumerate(series):
                if i in used:
                    continue
                if ps.column_name and ps.column_name == f:
                    result[f] = ps
                    used.add(i)
                    break

        # Pass 2: anchor-value match
        if anchor_data:
            for f in fields:
                if f in result:
                    continue
                known = anchor_data.get(f, {})
                if not known:
                    continue
                best_ps = None
                best_matches = 0
                for i, ps in enumerate(series):
                    if i in used:
                        continue
                    matches = sum(
                        1 for k, v in known.items()
                        if k in ps.lookup and _values_match(ps.lookup[k], v)
                    )
                    if matches > best_matches:
                        best_matches = matches
                        best_ps = ps
                        best_idx = i
                if best_ps and best_matches > 0:
                    result[f] = best_ps
                    used.add(best_idx)

        # Pass 3: order fallback for remaining unmatched fields
        remaining_series = [ps for i, ps in enumerate(series) if i not in used]
        remaining_fields = [f for f in fields if f not in result]
        for f, ps in zip(remaining_fields, remaining_series):
            result[f] = ps

        return result

    # ── Mode 1: construct numbers from scratch ──
    if not numbers and y_fields:
        field_map = _match_series_to_fields(y_fields, matching)
        if not field_map:
            return

        # Collect all unique keys in temporal/natural order
        all_keys: list = []
        seen: set = set()
        for ps in field_map.values():
            for k in ps.lookup:
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)

        built = []
        for k in all_keys:
            row: dict = {key_field: k}
            for yf in y_fields:
                ps = field_map.get(yf)
                row[yf] = ps.lookup.get(k) if ps else None
            built.append(row)
        kp_dict["numbers"] = built
        return

    if not numbers:
        return

    # ── Mode 2: fill nulls in existing numbers ──
    value_fields = y_fields or [
        k for k in numbers[0]
        if k != key_field and not k.startswith("threshold")
    ]

    has_nulls = any(
        row.get(f) is None
        for row in numbers if isinstance(row, dict)
        for f in value_fields
    )
    if not has_nulls:
        return

    # Build anchor data for matching
    anchor_data: dict[str, dict] = {}
    for field in value_fields:
        known = {}
        for row in numbers:
            if isinstance(row, dict) and row.get(field) is not None:
                known[row[key_field]] = row[field]
        if known:
            anchor_data[field] = known

    field_map = _match_series_to_fields(value_fields, matching, anchor_data)

    # Fill nulls
    for row in numbers:
        if not isinstance(row, dict):
            continue
        k = row.get(key_field)
        if k is None:
            continue
        for field in value_fields:
            if row.get(field) is not None:
                continue
            ps = field_map.get(field)
            if ps and k in ps.lookup:
                row[field] = ps.lookup[k]


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
            kb, turn_id, catalog, logger,
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
    kb, turn_id, catalog, logger,
) -> int:
    from tools.viz_renderer import kp_to_vega_spec, render_chart

    charts_rendered = 0

    # ── Trend charts ──
    # Build chart groups:
    #   1 series → single "trend" chart
    #   2 series → "trend_dual" (side-by-side comparison)
    #   3+ series → individual "trend" charts (separate tabs in the UI)
    if trend_series:
        if len(trend_series) == 2:
            # Dual-axis: combine into one chart
            chart_groups = [("trend_dual", trend_series)]
        elif len(trend_series) >= 3:
            # Individual charts — each series gets its own plot/tab
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
                cols = "_".join(ps.column_name for ps in group if ps.column_name)
                topic = f"{name}_{cols}_trajectory" if cols else f"{name}_dual_trend"
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

            # Inject thresholds from catalog
            if catalog and hasattr(catalog, "get_thresholds"):
                for table_name in catalog.list_tables():
                    thresholds = catalog.get_thresholds(table_name)
                    for yf in y_fields:
                        th_key = f"threshold_{yf}" if len(y_fields) > 1 else "threshold"
                        if yf in thresholds and not any(
                            p.get(th_key) is not None for p in points
                        ):
                            for p in points:
                                p[th_key] = thresholds[yf]["value"]

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
            "source_call": f"summarize_by_group('{ps.key_field}', op='sum')",
            "captured_at_turn": turn_id,
            "confidence": "high",
        }
        spec = kp_to_vega_spec(kp_dict)
        if spec:
            kp_dict["vega_spec"] = spec
        img_path = render_chart(kp_dict, charts_dir, turn_id=turn_id, logger=logger)
        if img_path:
            kp_dict["image_path"] = img_path
            kb.setdefault(name, []).append(kp_dict)
            charts_rendered += 1

    return charts_rendered


def _extract_data_tool_outputs(result) -> str:
    """Extract raw tool outputs from the specialist's conversation.

    The distiller needs the full series data from summarize_trend /
    summarize_by_group to populate `numbers` faithfully (all data points,
    not just the anchor values mentioned in the claim). Without these,
    the distiller sees only the SpecialistOutput summary and fills most
    periods with null.
    """
    if not hasattr(result, "to_input_list"):
        return ""
    outputs = []
    for item in result.to_input_list():
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            text = item.get("output", "")
            if text and len(text) > 50:
                outputs.append(text)
    if not outputs:
        return ""
    return "\n\n".join(outputs)


async def _distill_and_persist(
    app_ctx, name: str, sub_question: str, specialist_output,
    tool_outputs: str = "",
) -> int:
    """Run the distiller agent on a successful SpecialistOutput, append any
    extracted KnowledgePoints to the session KB. Returns count added.

    Failures are logged and non-fatal: the specialist's answer is already
    flowing to the orchestrator regardless. The session KB just doesn't get
    a new entry this turn — the specialist will still answer the next
    question, just without the new fact in its preface digest.
    """
    distiller = getattr(app_ctx, "_distiller", None)
    kb = getattr(app_ctx, "_specialist_kb", None)
    logger = getattr(app_ctx, "logger", None)
    node_store = getattr(app_ctx, "_node_trace_store", None)

    if distiller is None or kb is None:
        if logger is not None:
            logger.log("distiller_skipped", {
                "specialist": name,
                "reason": "not_wired",
                "distiller_none": distiller is None,
                "kb_none": kb is None,
            })
        return 0

    if name == "report_agent":
        if logger is not None:
            logger.log("distiller_skipped", {
                "specialist": name,
                "reason": "non_specialist_output_shape",
            })
        return 0

    # Narrow outputs → direct KB insertion with a node trace entry
    # so it's visible in the trace viewer.
    if _is_narrow_output(specialist_output, sub_question):
        findings = getattr(specialist_output, "findings", "") or ""
        sq_hash = hashlib.md5(sub_question.encode()).hexdigest()[:8]
        kp_dict = {
            "topic": f"{name}_q_{sq_hash}",
            "claim": findings,
            "numbers": [],
            "source_call": "",
            "confidence": "high",
        }
        turn_id = getattr(app_ctx, "_turn_id", None)
        if turn_id is not None:
            kp_dict["captured_at_turn"] = turn_id
        sess_list = kb.setdefault(name, [])
        sess_list.append(kp_dict)
        if logger is not None:
            logger.log("distiller_direct_kp", {
                "specialist": name,
                "reason": "narrow_output_direct_insert",
                "topic": kp_dict["topic"],
                "claim": findings[:200],
                "turn_id": turn_id,
            })
        # Create a node trace entry so the viewer shows it
        async with _open_node(node_store, f"distiller.{name}", depth=0):
            attach_tag("direct_insert")
            attach_extra(
                topic=kp_dict["topic"],
                claim=findings[:100],
                outcome="direct_insert",
                n_added=1,
            )
        return 1

    timer = ProcessTimer(
        logger,
        "distiller",
        turn_id=getattr(app_ctx, "_turn_id", None),
        specialist=name,
    )

    # Pack a compact, JSON-serializable view of the specialist's output for
    # the distiller's prompt. SpecialistOutput is a Pydantic model on the
    # success path; on failures we'd be a "[FAILED ...]" string, but we
    # only get here on success so that branch is paranoia.
    t0 = time.perf_counter()
    try:
        if hasattr(specialist_output, "model_dump"):
            output_payload = json.dumps(specialist_output.model_dump(), default=str)
        elif isinstance(specialist_output, str):
            output_payload = specialist_output
        else:
            output_payload = json.dumps(specialist_output, default=str)
    except Exception:
        output_payload = str(specialist_output)
    timer.record(
        "distiller_input_serialize",
        int((time.perf_counter() - t0) * 1000),
        payload_chars=len(output_payload),
    )

    distiller_input = (
        f"Specialist: {name}\n"
        f"Sub-question: {sub_question}\n\n"
        f"--- SpecialistOutput (JSON) ---\n{output_payload}"
    )
    # Raw tool outputs are NOT included in the distiller input — they
    # inflate it by 5-10K tokens and add 10-15s TTFT. Instead, the
    # post-fill step (`_fill_kp_numbers`) programmatically fills the
    # `numbers` array from parsed tool outputs after the distiller runs.
    # The distiller only needs the SpecialistOutput to decide topic,
    # claim, viz kind, and confidence.

    try:
        t0 = time.perf_counter()
        # Route distiller LLM calls to the SPECIALIST semaphore pool
        # (12 slots) instead of the orchestrator pool (2 slots). Without
        # this, the distiller and orchestrator synthesis compete for the
        # same 2 slots — serializing what should run in parallel.
        kind_token = LLM_CALL_KIND.set("specialist")
        node_store = getattr(app_ctx, "_node_trace_store", None)
        try:
            async with _open_node(node_store, f"distiller.{name}", depth=0):
                result = await asyncio.wait_for(
                    Runner.run(distiller, distiller_input, context=app_ctx, max_turns=1),
                    timeout=_DISTILLER_TIMEOUT_S,
                )
        finally:
            LLM_CALL_KIND.reset(kind_token)
        timer.record(
            "distiller_runner",
            int((time.perf_counter() - t0) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - distillation is best-effort
        if logger is not None:
            logger.log("distiller_failed", {
                "specialist": name,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            })
        timer.summary(outcome="failed", error_type=type(exc).__name__)
        return 0

    out = getattr(result, "final_output", None)
    new_kps = getattr(out, "knowledge_points", None) or []
    if not isinstance(new_kps, list):
        timer.summary(outcome="no_kps", n_added=0)
        return 0

    # Pre-parse series from tool outputs for post-fill (fills null values
    # the distiller left in `numbers` with real data from tool results).
    parsed_series = _parse_series_from_tool_outputs(tool_outputs)

    turn_id = getattr(app_ctx, "_turn_id", None)
    case_folder = getattr(app_ctx, "case_folder", None)
    sess_list = kb.setdefault(name, [])
    added_topics: list[str] = []
    n_with_charts = 0
    n_nulls_filled = 0
    render_total_ms = 0
    t0 = time.perf_counter()
    for kp in new_kps:
        try:
            kp_dict = kp.model_dump() if hasattr(kp, "model_dump") else dict(kp)
        except Exception:
            continue
        if turn_id is not None and not kp_dict.get("captured_at_turn"):
            kp_dict["captured_at_turn"] = turn_id

        # Post-fill: fill/construct numbers from parsed tool outputs.
        # Mode 1: numbers is empty → construct full array from series.
        # Mode 2: numbers has nulls → fill from matched series.
        has_viz = isinstance(kp_dict.get("viz"), dict) and kp_dict["viz"].get("y_fields")
        if parsed_series and (kp_dict.get("numbers") or has_viz):
            before_len = len(kp_dict.get("numbers") or [])
            before = sum(
                1 for row in kp_dict["numbers"] if isinstance(row, dict)
                for v in row.values() if v is None
            ) if kp_dict.get("numbers") else 0
            _fill_kp_numbers(kp_dict, parsed_series)
            after_len = len(kp_dict.get("numbers") or [])
            after = sum(
                1 for row in kp_dict["numbers"] if isinstance(row, dict)
                for v in row.values() if v is None
            ) if kp_dict.get("numbers") else 0
            n_nulls_filled += (before - after) + (after_len - before_len)
        elif has_viz and not kp_dict.get("numbers") and logger is not None:
            logger.log("distiller_kp_no_numbers", {
                "specialist": name,
                "topic": kp_dict.get("topic", ""),
                "has_parsed_series": bool(parsed_series),
                "n_parsed_series": len(parsed_series),
                "tool_outputs_chars": len(tool_outputs),
            })

        # Charts are now the SPECIALIST's responsibility (via make_chart
        # tool call with real data). The distiller only handles KB warmth
        # (claims/topics for follow-up questions). No chart rendering here.

        sess_list.append(kp_dict)
        if kp_dict.get("topic"):
            added_topics.append(kp_dict["topic"])

    if added_topics and logger is not None:
        logger.log("distiller_kps_added", {
            "specialist": name,
            "n_added": len(added_topics),
            "kb_size_now": len(sess_list),
            "topics": added_topics,
            "n_with_charts": sum(1 for k in sess_list[-len(added_topics):]
                                 if k.get("image_path")),
        })
    timer.record(
        "kp_persist_and_render",
        int((time.perf_counter() - t0) * 1000),
        n_kps=len(new_kps),
        n_added=len(added_topics),
        n_with_charts=n_with_charts,
        render_total_ms=render_total_ms,
    )
    timer.summary(
        outcome="ok",
        n_added=len(added_topics),
        n_with_charts=n_with_charts,
        n_nulls_filled=n_nulls_filled,
    )
    return len(added_topics)


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


def redacting_tool(agent: Agent, name: str, description: str):
    """Return a FunctionTool that runs ``agent`` with input/output redaction.

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
                kb_digest = _format_kb_digest(kps_for_name)
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
                    contextual_in = (
                        f"[Case folder files: {', '.join(files)}]\n\n"
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

        # Wall-clock + turn-budget + exception fence around the inner run.
        # Without these, a hung LLM / network layer or any non-MaxTurnsExceeded
        # SDK error (ModelBehaviorError, output-schema parse failure, transport
        # error) escapes to function_tool's default failure handler, which
        # returns a generic "An error occurred while running the tool" string
        # — the orchestrator then renders it as "specialist did not return"
        # and the reviewer never sees the real cause. We catch each class
        # explicitly, log it, and return a structured ``[FAILED …]`` payload.
        try:
            t0 = time.perf_counter()
            # Mark every LLM call originating from inside this
            # specialist's Runner.run as "specialist"-kind. The
            # firewall stack routes these to the specialist semaphore
            # pool (FIREWALL_SPECIALIST_CONCURRENCY, default 8),
            # leaving the orchestrator pool reserved for the
            # team-planning / synthesis calls that happen outside any
            # specialist context. Without this routing, a Round-1 burst
            # of 3 specialists × 4-6 internal LLM calls each piled up
            # behind a single 3-slot semaphore, serializing work that
            # should be parallel.
            kind_token = LLM_CALL_KIND.set("specialist")
            node_store = getattr(app_ctx, "_node_trace_store", None)
            try:
                node_label = name if name == "report_agent" else f"specialist.{name}"
                async with _open_node(node_store, node_label, depth=0):
                    if kb_digest_n_kps:
                        attach_tag("kb_digest_present")
                        attach_extra(n_kps_in_digest=kb_digest_n_kps)
                    if prior:
                        attach_tag("warm_specialist")
                    result = await asyncio.wait_for(
                        Runner.run(
                            inner, run_input, context=app_ctx,
                            max_turns=_SPECIALIST_MAX_TURNS,
                        ),
                        timeout=_SPECIALIST_TIMEOUT_S,
                    )
            finally:
                LLM_CALL_KIND.reset(kind_token)
            timer.record(
                "specialist_runner",
                int((time.perf_counter() - t0) * 1000),
                max_turns=_SPECIALIST_MAX_TURNS,
            )
        except MaxTurnsExceeded as exc:
            timer.summary(
                outcome="failed",
                error_type="max_turns_exceeded",
                total_ms=int((time.perf_counter() - runner_started) * 1000),
            )
            return _record_failure(
                app_ctx, name, redacted_in,
                "max_turns_exceeded",
                f"hit the {_SPECIALIST_MAX_TURNS}-turn budget — "
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
                f"{_SPECIALIST_TIMEOUT_S:.0f}s wall-clock budget.",
                exc,
            )
        except AgentsException as exc:
            # Covers ModelBehaviorError (malformed JSON / nonexistent tool /
            # output-schema parse failure), UserError (SDK misuse), and
            # guardrail tripwires.
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
            # Network / transport / serialization / anything else. We don't
            # want a stray exception class to slip past and surface as the
            # SDK's generic paraphrase.
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
