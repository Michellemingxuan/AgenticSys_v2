"""Series extraction + KP-number filling shared by the distiller pass and the
auto-chart renderer. Parses summarize_trend / batch_summarize_trend /
summarize_by_group tool outputs into `_ParsedSeries`, and fills/constructs a
KnowledgePoint `numbers` array from them. Pure data plumbing — no LLM, no I/O.
Extracted from tools/agent_tool.py (see
docs/superpowers/specs/2026-07-05-redacting-tool-decomposition-design.md)."""
from __future__ import annotations

import dataclasses
import json


@dataclasses.dataclass
class _ParsedSeries:
    """A parsed data series with optional column-name metadata."""
    lookup: dict  # {period_or_group: value}
    column_name: str  # from batch_summarize_trend's value_column, or ""
    key_field: str  # "period" or "group" or "merchant"
    table_name: str = ""  # source table for disambiguation


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

    def _series_to_parsed(series: list[dict], column_name: str = "",
                          table_name: str = "") -> _ParsedSeries | None:
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
                             key_field=key_field,
                             table_name=table_name) if lookup else None

    for parsed in _extract_json_objects(tool_outputs_text):
        # summarize_trend: {series: [...], summary: {...}, value_column: "...", table: "..."}
        s = parsed.get("series")
        if isinstance(s, list) and len(s) >= 2:
            col = str(parsed.get("value_column") or "")
            tbl = str(parsed.get("table") or "")
            ps = _series_to_parsed(s, column_name=col, table_name=tbl)
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
                tbl = ""
                # Try direct "series" key first (future-proofing)
                s = r.get("series")
                # If not found, parse the "result" string
                if s is None and isinstance(r.get("result"), str):
                    try:
                        inner = json.loads(r["result"])
                        if isinstance(inner, dict):
                            s = inner.get("series")
                            tbl = str(inner.get("table") or "")
                    except (json.JSONDecodeError, ValueError):
                        pass
                if isinstance(s, list) and len(s) >= 2:
                    ps = _series_to_parsed(s, column_name=str(col),
                                           table_name=tbl)
                    if ps:
                        results.append(ps)

        # summarize_by_group: {groups: [...], concentration: {...},
        #   group_column: "...", value_column: "..."}
        g = parsed.get("groups")
        if isinstance(g, list) and len(g) >= 2:
            tbl = str(parsed.get("table") or "")
            grp_col = str(parsed.get("group_column") or "")
            val_col = str(parsed.get("value_column") or "")
            col_label = f"{val_col}_by_{grp_col}" if grp_col else val_col
            ps = _series_to_parsed(g, column_name=col_label,
                                   table_name=tbl)
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
