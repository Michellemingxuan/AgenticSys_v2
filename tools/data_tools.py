"""Data-access tool functions for agent tool-calling.

All queries are scoped to the currently active case. The case_id is set on the
gateway at session start — tools don't need to specify it.
"""

from __future__ import annotations

import json
import operator
import re
from typing import Any, Callable

from agents import function_tool
from datalayer.catalog import DataCatalog
from datalayer.gateway import DataGateway

# Module state — guarded against autoreload reset.
# In notebooks with `%autoreload 2`, re-executing this module's top level
# would reset these to None and silently break the session (the gateway the
# notebook just initialized would vanish). The try/except preserves whatever
# `init_tools()` last set across reloads.
try:
    _gateway  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _gateway: DataGateway | None = None
try:
    _catalog  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _catalog: DataCatalog | None = None
try:
    _logger  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _logger: Any = None  # logger.event_logger.EventLogger when wired; None = silent

_MAX_CHARS = 3000
_LOG_PREVIEW_CHARS = 500  # how much of tool output to snapshot in tool_result events

_FILTER_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


# Lightweight table-name normalization for real→canonical resolution.
# Mirrors datalayer.adapter._normalize_name without importing the adapter
# module (which pulls pandas — sync-time only).
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_TRAILING_DIGITS = re.compile(r"\d+$")


def _normalize(name: str) -> str:
    return _TRAILING_DIGITS.sub("", _NON_ALNUM.sub("", name.lower()))


def _resolve_canonical_table(real_table: str) -> str | None:
    """Find the primary canonical table name that matches a real table name.

    Returns the highest-priority match from the cascade in
    :func:`_resolve_canonical_tables` (or ``None`` if nothing matches).
    """
    matches = _resolve_canonical_tables(real_table)
    return matches[0] if matches else None


def _resolve_canonical_tables(real_table: str) -> list[str]:
    """Find all canonical tables relevant to a real table, in priority order.

    Matching cascade:
      1. Exact key in catalog ``_profiles``.
      2. Table-level ``aliases`` declared in any canonical profile (e.g.
         ``model_scores.yaml`` declares ``aliases: [modelling_data]``).
      3. Equal under normalization (case/punctuation only).
      4. Substring overlap of normalized forms (``bureau`` ⊂ ``bureau_data``).

    Returns a deduped list — the first entry is the primary match, the rest
    are fallbacks. Useful when a hand-written real-data profile (like
    ``bureau_data.yaml``) only carries a subset of columns and the rest
    need to be looked up under the broader canonical (``bureau.yaml``).
    """
    if _catalog is None:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            out.append(name)

    if real_table in _catalog._profiles:
        _add(real_table)

    # Stage 2: table-level aliases.
    for canonical, profile in _catalog._profiles.items():
        if real_table in (profile.get("aliases") or []):
            _add(canonical)

    real_norm = _normalize(real_table)
    for canonical in _catalog._profiles:
        if _normalize(canonical) == real_norm:
            _add(canonical)
    for canonical in _catalog._profiles:
        canonical_norm = _normalize(canonical)
        if canonical_norm and (canonical_norm in real_norm or real_norm in canonical_norm):
            _add(canonical)
    return out


_MONTHS: dict[str, int] = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
# also accept 3-letter abbreviations
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")
# Month-year, separator is one of `'`, `-`, or whitespace:
# "October'2024", "October 2024", "Oct'2024", "Oct 2024", "Jan-2024".
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,})\s*[-'\s]\s*(\d{4})$")
# DD-MMM-YYYY: "07-Jul-2024", "7-Jul-2024".
_DAY_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3,})-(\d{4})$")


def _date_key(value: Any) -> tuple[int, int, int] | None:
    """Parse common date / period string formats into a comparable
    (year, month, day) tuple. Returns None if unparseable.

    Handles formats produced across the data profiles:
      - ``2025-11-16``                                     → (2025, 11, 16)
      - ``07-Jul-2024`` / ``7-Jul-2024``                   → (2024, 7, 7)
      - ``2025-11``                                        → (2025, 11, 1)
      - ``October'2024`` / ``October 2024`` / ``Oct'2024`` → (2024, 10, 1)
      - ``Jan-2024`` / ``Jan 2024`` / ``January-2024``     → (2024, 1, 1)
      - ``2025``                                           → (2025, 1, 1)
    Tuple comparison matches chronological order for any of these.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    m = _ISO_DATE_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD-MMM-YYYY (must come before _ISO_MONTH_RE since both contain hyphens
    # but this one starts with a 1-2 digit day).
    m = _DAY_MONTH_YEAR_RE.match(s)
    if m:
        month_idx = _MONTHS.get(m.group(2).lower())
        if month_idx is not None:
            return (int(m.group(3)), month_idx, int(m.group(1)))

    m = _ISO_MONTH_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), 1)

    m = _MONTH_YEAR_RE.match(s)
    if m:
        month_idx = _MONTHS.get(m.group(1).lower())
        if month_idx is not None:
            return (int(m.group(2)), month_idx, 1)

    m = _YEAR_RE.match(s)
    if m:
        return (int(m.group(1)), 1, 1)

    return None


def _coerce_pair(a: Any, b: Any) -> tuple[Any, Any]:
    """Best-effort comparable coercion: numeric → date-tuple → string.

    Ensures ISO dates, YYYY-MM month strings, plain years, and the
    ``MonthName'YYYY`` format all compare chronologically. For everything
    else, falls back to string comparison.
    """
    # 1) numeric
    try:
        return float(a), float(b)
    except (TypeError, ValueError):
        pass
    # 2) date-ish — only if BOTH sides parse, so a mixed pair doesn't
    #    quietly mis-compare.
    ak, bk = _date_key(a), _date_key(b)
    if ak is not None and bk is not None:
        return ak, bk
    # 3) string fallback
    return (str(a) if a is not None else ""), (str(b) if b is not None else "")


def _resolve_real_table(requested: str) -> str:
    """Resolve a requested table name to whatever the gateway actually carries.

    Specialists call query_table with canonical names from skill data_hints
    (e.g. ``crossbu_cards``) but real CSVs may use a slightly different name
    (``crossbu_cards_data``). This walks: gateway exact → catalog table-level
    aliases (canonical → real) → normalized fuzzy. Falls through unchanged
    when nothing matches.
    """
    if _gateway is None or not requested:
        return requested
    real_tables = _gateway.list_tables() if _gateway.get_case_id() else []
    if not real_tables:
        return requested
    if requested in real_tables:
        return requested

    # Canonical → real via catalog's declared table-level aliases.
    if _catalog is not None:
        aliases = _catalog.table_aliases(requested)
        for alias in aliases:
            if alias in real_tables:
                return alias

    # Normalized fuzzy fallback.
    target = _normalize(requested)
    if not target:
        return requested
    for real in real_tables:
        if _normalize(real) == target:
            return real
    return requested


def _resolve_real_column(
    rows: list[dict],
    requested: str,
    table_name: str | None = None,
) -> str:
    """Resolve a requested column name to the actual key used in rows.

    Lookup order:
      1. Exact match in the row's real keys.
      2. Catalog-declared aliases — most authoritative; ``payments.yaml``
         declares e.g. ``return_flag.aliases: [Return Flag]`` so a specialist
         passing the canonical ``return_flag`` resolves to ``Return Flag`` in
         the real CSV.
      3. Normalization-based fuzzy match (case + punctuation only) as a
         fallback for variants the catalog hasn't declared.

    Falls through (returns the input) when nothing matches — the filter or
    projection will then return zero rows / drop the column, which is the
    right behavior for a genuinely-missing column.
    """
    if not rows or not requested:
        return requested
    real_keys = list(rows[0].keys())
    if requested in real_keys:
        return requested

    if _catalog is not None and table_name:
        canonical_table = _resolve_canonical_table(table_name) or table_name
        resolved = _catalog.resolve_real_column(canonical_table, requested, real_keys)
        if resolved != requested and resolved in real_keys:
            return resolved

    target = _normalize(requested)
    if not target:
        return requested
    for k in real_keys:
        if _normalize(k) == target:
            return k
    return requested


def _apply_filter(
    rows: list[dict],
    column: str,
    value: str,
    op: str,
) -> list[dict]:
    """Filter rows by column using the named comparison operator.

    Supported ops: eq, ne, gt, gte, lt, lte, between.
    For ``between``, ``value`` must be "<low>,<high>" (inclusive bounds).
    """
    op = (op or "eq").lower()
    if op == "between":
        parts = [v.strip() for v in str(value).split(",") if v.strip()]
        if len(parts) != 2:
            return rows
        lo, hi = parts
        out: list[dict] = []
        for r in rows:
            cell = r.get(column)
            if cell is None:
                continue
            a_lo, b_lo = _coerce_pair(cell, lo)
            a_hi, b_hi = _coerce_pair(cell, hi)
            if a_lo >= b_lo and a_hi <= b_hi:
                out.append(r)
        return out

    cmp = _FILTER_OPS.get(op)
    if cmp is None:
        return rows
    out = []
    for r in rows:
        cell = r.get(column)
        if cell is None:
            continue
        a, b = _coerce_pair(cell, value)
        if cmp(a, b):
            out.append(r)
    return out


def init_tools(gateway: DataGateway, catalog: DataCatalog, logger: Any = None) -> None:
    """Initialize the module-level tool state.

    ``logger`` is optional; when provided (typically an ``EventLogger``),
    every tool invocation emits a ``tool_call`` event (with args) and a
    ``tool_result`` event (with row count + preview of the returned string)
    so the data pipeline is visible in the session log.
    """
    global _gateway, _catalog, _logger
    _gateway = gateway
    _catalog = catalog
    _logger = logger


def set_logger(logger: Any) -> None:
    """Attach (or detach) a logger after ``init_tools`` has been called.

    Useful in notebooks where data is loaded before the session logger is
    constructed. Pass ``None`` to silence logging.
    """
    global _logger
    _logger = logger


def _log_call(tool: str, args: dict[str, Any]) -> None:
    if _logger is not None:
        _logger.log("tool_call", {"tool": tool, "args": args})


def _log_result(
    tool: str,
    *,
    result: str,
    rows_returned: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if _logger is None:
        return
    preview = result if len(result) <= _LOG_PREVIEW_CHARS else result[:_LOG_PREVIEW_CHARS] + "…"
    payload: dict[str, Any] = {
        "tool": tool,
        "result_preview": preview,
        "result_chars": len(result),
    }
    if rows_returned is not None:
        payload["rows_returned"] = rows_returned
    if extra:
        payload.update(extra)
    _logger.log("tool_result", payload)


def _list_available_tables_impl() -> str:
    """List all data tables available for the current case, each with its description."""
    _log_call("list_available_tables", {})
    if _catalog is None:
        out = "Data unavailable"
        _log_result("list_available_tables", result=out)
        return out

    def _render(tables: list[str]) -> str:
        lines: list[str] = []
        for t in tables:
            canonical = _resolve_canonical_table(t) or t
            desc = _catalog.get_description(canonical) if _catalog else ""
            label = (
                f"{t} [canonical: {canonical}]"
                if canonical != t and desc
                else t
            )
            if desc:
                lines.append(f"- {label}: {desc}")
            else:
                lines.append(f"- {label}")
        return "\n".join(lines)

    if _gateway is not None and _gateway.get_case_id() is not None:
        case_tables = _gateway.list_tables()
        if case_tables:
            out = "Tables for the current case:\n" + _render(case_tables)
            _log_result("list_available_tables", result=out,
                        extra={"table_count": len(case_tables)})
            return out
        out = "No tables available for the current case."
        _log_result("list_available_tables", result=out,
                    extra={"table_count": 0})
        return out

    tables = _catalog.list_tables()
    out = _render(tables) if tables else "No tables available"
    _log_result("list_available_tables", result=out,
                extra={"table_count": len(tables)})
    return out


@function_tool
def list_available_tables() -> str:
    """List all data tables available for the current case, each with its description."""
    return _list_available_tables_impl()


def _get_table_schema_impl(table_name: str) -> str:
    """Get the column schema for a specific table.

    When a case is active, the schema is filtered to only the columns
    physically present in the case's CSV (i.e., the simulated catalog's
    extra columns are hidden). Each real column is annotated with the
    canonical column's dtype + description if a match exists in the
    canonical profile (via name, alias, or normalized fuzzy match).
    Columns present in the CSV but absent from the canonical are emitted
    with ``type: unknown`` and a "(not in catalog)" description so the
    LLM still sees they exist.
    """
    _log_call("get_table_schema", {"table_name": table_name})
    if _catalog is None:
        out = "Data unavailable"
        _log_result("get_table_schema", result=out)
        return out

    if _gateway is not None and _gateway.get_case_id() is not None:
        # Resolve canonical → real table name (specialists may pass either).
        real_table = _resolve_real_table(table_name)
        rows = _gateway.query(real_table) or []
        if not rows:
            out = f"Data unavailable: table '{table_name}' not found for current case."
            _log_result("get_table_schema", result=out,
                        extra={"table_name": table_name, "found": False})
            return out
        if real_table != table_name:
            table_name = real_table

        canonical_tables = _resolve_canonical_tables(table_name)
        # Build a merged column-spec map across all matching canonical tables.
        # Earlier entries win, so a hand-written real-data profile takes
        # precedence over the broader canonical it shares a name with.
        merged_cols: dict[str, dict] = {}
        canonical_lookup: dict[str, str] = {}  # col_name → canonical name
        for ct in canonical_tables:
            for col, spec in (_catalog._profiles.get(ct, {}).get("columns", {}) or {}).items():
                merged_cols.setdefault(col, spec)
                canonical_lookup.setdefault(col, col)

        # Add table-level aliases preface so the LLM sees the table is the
        # rbind of multiple sources when applicable.
        table_aliases: list[str] = []
        for ct in canonical_tables:
            table_aliases.extend(_catalog.table_aliases(ct))

        schema: dict[str, dict] = {}
        for real_col in rows[0].keys():
            spec = _find_column_spec(merged_cols, real_col)
            if spec is not None:
                # Determine the canonical name for this real column.
                if real_col in merged_cols:
                    canonical = real_col
                else:
                    canonical = next(
                        (c for c, s in merged_cols.items()
                         if real_col in (s.get("aliases") or [])
                         or _normalize(c) == _normalize(real_col)
                         or any(_normalize(a) == _normalize(real_col)
                                for a in (s.get("aliases") or []))),
                        real_col,
                    )
                entry: dict = {
                    "type": spec.get("dtype", "unknown"),
                    "description": spec.get("description", ""),
                }
                if canonical != real_col:
                    entry["canonical_name"] = canonical
                aliases = spec.get("aliases") or []
                if aliases:
                    entry["aliases"] = list(aliases)
                # Surface declared categorical values when the profile has
                # them — helps specialists pick the right filter_value
                # vocabulary. NOTE: these are example/reference values from
                # the catalog (post-sync they may reflect real-data
                # observation), NOT an authoritative scope for inference.
                # Specialists must probe the actual data when in doubt; see
                # the SCHEMA & VOCABULARY DISCIPLINE rules in data_query.md.
                if "categories" in spec:
                    entry["declared_values"] = list(spec["categories"].keys())
                schema[real_col] = entry
            else:
                schema[real_col] = {"type": "unknown", "description": "(not in catalog)"}

        if table_aliases:
            schema["__table_aliases__"] = table_aliases

        out = json.dumps(schema, indent=2)
        _log_result("get_table_schema", result=out,
                    extra={"table_name": table_name, "found": True,
                           "canonical": canonical_tables[0] if canonical_tables else None,
                           "canonical_chain": canonical_tables,
                           "column_count": len(schema)})
        return out

    schema = _catalog.get_schema(table_name)
    if schema is None:
        out = "Data unavailable"
        _log_result("get_table_schema", result=out,
                    extra={"table_name": table_name, "found": False})
        return out
    out = json.dumps(schema, indent=2)
    _log_result("get_table_schema", result=out,
                extra={"table_name": table_name, "found": True,
                       "column_count": len(schema)})
    return out


@function_tool
def get_table_schema(table_name: str) -> str:
    """Get the column schema for a specific table.

    When a case is active, the schema is filtered to only the columns
    physically present in the case's CSV. Each real column is annotated with
    the canonical column's dtype + description if a match exists in the
    canonical profile.
    """
    return _get_table_schema_impl(table_name)


def _find_column_spec(canonical_cols: dict, real_col: str) -> dict | None:
    """Return the canonical spec matching a real column name (or None).

    Checks: exact key, alias list, normalized form across both.
    """
    if real_col in canonical_cols:
        return canonical_cols[real_col]
    real_norm = _normalize(real_col)
    for spec in canonical_cols.values():
        if real_col in (spec.get("aliases") or []):
            return spec
    for canonical_col, spec in canonical_cols.items():
        if _normalize(canonical_col) == real_norm:
            return spec
        for alias in spec.get("aliases") or []:
            if _normalize(alias) == real_norm:
                return spec
    return None


def _query_table_impl(
    table_name: str,
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    columns: str = "",
) -> str:
    """Query a data table for the current case. All data is scoped to the active case.

    Returns a JSON object with structured count metadata and a sample of rows:
        {
          "table": "<name>",
          "filter": "<col> <op> <value>" or null,
          "columns_requested": [...] or null,
          "total_rows_in_table": int,    # rows in the table for this case
          "rows_matching_filter": int,   # rows after filter — the TRUE count
          "rows_returned": int,          # rows actually included in `rows` (may be truncated)
          "truncated": bool,
          "truncation_note": "showing 4/186 rows, 6/12 columns" (only if truncated),
          "rows": [ {...}, ... ]
        }

    For "how many" questions: ALWAYS read `rows_matching_filter`. NEVER count
    the entries in the `rows` array — that array is a display sample and may
    be truncated when the table is large or rows are wide. The sample lets
    you verify shape and pick representative values; the count comes from
    `rows_matching_filter`.

    Args:
        table_name: the table to query.
        filter_column: column to filter on (optional).
        filter_value: value(s) for the filter. For ``filter_op="between"`` pass
            "<low>,<high>" (inclusive). For ISO dates (YYYY-MM-DD) and YYYY-MM
            strings, lexicographic order matches chronological order.
        filter_op: one of "eq" (default), "ne", "gt", "gte", "lt", "lte", "between".
            Use range ops for time windows — e.g. for payments in the 3 months
            before cut-off 2025-12-01, call:
                query_table("payments", filter_column="payment_date",
                            filter_op="gte", filter_value="2025-09-01")
            Or use "between" to bound both sides.
        columns: comma-separated list of column names to return (e.g.
            "fico_score,derog_count"). Leave empty to return all columns.
            REQUIRED for wide tables like model_scores (266 cols) to avoid
            slow processing — request only the columns you need.
    """
    _log_call("query_table", {
        "table_name": table_name,
        "filter_column": filter_column,
        "filter_value": filter_value,
        "filter_op": filter_op if (filter_column and filter_value) else None,
        "columns": columns,
    })

    if _gateway is None:
        out = (
            "Data unavailable: data layer is not initialized for this session "
            "(no gateway is bound to tools.data_tools). This is an infrastructure "
            "error, NOT a finding about the case data — do not interpret it as "
            "'no data exists'. In a notebook, re-run the cell that calls "
            "init_tools(gateway, catalog) and gateway.set_case(case_id)."
        )
        _log_result("query_table", result=out,
                    extra={"reason": "no_gateway_bound"})
        return out

    # Fetch ALL rows for this case, then apply the filter in Python so we
    # can support range operators. The gateway itself only knows exact match.
    # Resolve canonical → real table name (e.g. 'crossbu_cards' →
    # 'crossbu_cards_data') so specialists can call with either name.
    real_table = _resolve_real_table(table_name)
    rows = _gateway.query(real_table, filters=None)
    if rows is None:
        out = f"Data unavailable: table '{table_name}' not found for current case."
        _log_result("query_table", result=out,
                    extra={"table_name": table_name, "found": False})
        return out
    # Use the resolved name in the response so the LLM sees the actual table.
    if real_table != table_name:
        table_name = real_table

    total_rows_in_table = len(rows)
    filter_descriptor: str | None = None
    resolved_filter_column: str | None = None
    if filter_column and filter_value:
        # Resolve case/space variants ('return_flag' → 'Return Flag') against
        # the real CSV headers before filtering. Without this, a specialist
        # following the skill's snake_case names silently gets 0 rows.
        resolved_filter_column = _resolve_real_column(rows, filter_column, table_name)
        rows = _apply_filter(rows, resolved_filter_column, str(filter_value), filter_op)
        if resolved_filter_column != filter_column:
            filter_descriptor = (
                f"{resolved_filter_column} {filter_op} {filter_value!r} "
                f"(resolved from '{filter_column}')"
            )
        else:
            filter_descriptor = f"{filter_column} {filter_op} {filter_value!r}"
    rows_matching_filter = len(rows)

    # Column projection — select only requested columns (with the same
    # case/space resolution as the filter column).
    requested_cols: list[str] | None = None
    if columns:
        requested = [c.strip() for c in columns.split(",") if c.strip()]
        if requested:
            requested_cols = requested
            if rows:
                resolved_map = {c: _resolve_real_column(rows, c, table_name) for c in requested}
                rows = [
                    {resolved_map[c]: row[resolved_map[c]]
                     for c in requested if resolved_map[c] in row}
                    for row in rows
                ]

    truncation_notes: list[str] = []

    if rows:
        total_cols = len(rows[0])
        # Step 1: trim columns if a single row is already too wide
        single_row_size = len(json.dumps([rows[0]], indent=2, default=str))
        if single_row_size > _MAX_CHARS - 600:
            keys = list(rows[0].keys())
            keep_keys: list[str] = []
            for k in keys:
                test_row = {kk: rows[0][kk] for kk in keep_keys + [k]}
                if len(json.dumps([test_row], indent=2, default=str)) > _MAX_CHARS - 700:
                    break
                keep_keys.append(k)
            rows = [{k: row[k] for k in keep_keys if k in row} for row in rows]
            truncation_notes.append(f"showing {len(keep_keys)}/{total_cols} columns")

        # Step 2: reduce rows until JSON fits, leaving room for the wrapper
        text = json.dumps(rows, indent=2, default=str)
        while len(text) > _MAX_CHARS - 500 and len(rows) > 1:
            rows = rows[: len(rows) // 2]
            text = json.dumps(rows, indent=2, default=str)
        if len(rows) < rows_matching_filter:
            truncation_notes.append(f"showing {len(rows)}/{rows_matching_filter} rows")

    rows_returned = len(rows)
    truncated = bool(truncation_notes)

    response: dict[str, Any] = {
        "table": table_name,
        "filter": filter_descriptor,
        "columns_requested": requested_cols,
        "total_rows_in_table": total_rows_in_table,
        "rows_matching_filter": rows_matching_filter,
        "rows_returned": rows_returned,
        "truncated": truncated,
        "rows": rows,
    }
    if truncated:
        response["truncation_note"] = ", ".join(truncation_notes)
        response["count_advice"] = (
            "rows_matching_filter is the true count; the rows array below is a "
            "display sample — do NOT count its entries for 'how many' questions."
        )

    out = json.dumps(response, indent=2, default=str)
    _log_result(
        "query_table", result=out, rows_returned=rows_returned,
        extra={
            "table_name": table_name,
            "rows_before_filter": total_rows_in_table,
            "rows_after_filter": rows_matching_filter,
            "rows_shown": rows_returned,
            "truncation": truncation_notes or None,
        },
    )
    return out


@function_tool
def query_table(
    table_name: str,
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    columns: str = "",
) -> str:
    """Query a data table for the current case. All data is scoped to the active case.

    Returns a JSON object: {table, filter, total_rows_in_table,
    rows_matching_filter, rows_returned, truncated, rows: [...]}.

    For 'how many' / count questions: ALWAYS read `rows_matching_filter` from
    the response. The `rows` array is a display sample that may be truncated
    when the table is large — counting its entries gives the wrong answer.

    Args:
        table_name: the table to query.
        filter_column: column to filter on (optional).
        filter_value: value(s) for the filter. For ``filter_op="between"`` pass
            "<low>,<high>" (inclusive). For ISO dates (YYYY-MM-DD) and YYYY-MM
            strings, lexicographic order matches chronological order.
        filter_op: one of "eq" (default), "ne", "gt", "gte", "lt", "lte", "between".
        columns: comma-separated list of column names to return (e.g.
            "fico_score,derog_count"). Leave empty to return all columns.
    """
    return _query_table_impl(
        table_name=table_name,
        filter_column=filter_column,
        filter_value=filter_value,
        filter_op=filter_op,
        columns=columns,
    )


# ── aggregate_column ──────────────────────────────────────────────────────
#
# Server-side aggregation tool. The redaction layer masks any 6+ digit run
# (`\d{6,}`) — so when an LLM tries to compose an answer like "the total
# balance is $174897.36", the boundary redact_payload turns it into
# "***MASKED***.36" because `174897` is six digits. Computing the aggregate
# in Python and formatting with thousand-separators ($174,897.36) sidesteps
# the regex (commas break the digit run) so the value survives unchanged
# through every redaction boundary. Specialists must use this tool for any
# total / mean / max / min / count question instead of summing rows mentally.

_MONEY_KEY = ("balance", "amount", "limit", "spend", "payment", "value", "exposure")


def _looks_like_money(column: str) -> bool:
    c = (column or "").lower()
    return any(k in c for k in _MONEY_KEY)


def _format_aggregate(value, column: str, op: str) -> str:
    """Format an aggregate result so it survives 6+ digit redaction.

    Always uses thousand separators. Prepends '$' for monetary-looking
    columns. Counts are integer; sums/means/max/min on monetary columns
    show two decimal places.
    """
    if value is None:
        return "(no data)"
    is_money = _looks_like_money(column) and op != "count"
    if op == "count" or (isinstance(value, (int, float)) and float(value).is_integer()
                         and not is_money):
        formatted = f"{int(value):,}"
    else:
        formatted = f"{value:,.2f}"
    return f"${formatted}" if is_money else formatted


def _aggregate_column_impl(
    table_name: str,
    column: str,
    op: str = "sum",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    """Compute an aggregate over a column, returning a formatted string."""
    op = (op or "sum").lower()
    _log_call("aggregate_column", {
        "table_name": table_name, "column": column, "op": op,
        "filter_column": filter_column, "filter_value": filter_value,
        "filter_op": filter_op if (filter_column and filter_value) else None,
    })

    if _gateway is None:
        out = (
            "Data unavailable: data layer is not initialized for this session. "
            "Infrastructure error, not a data finding."
        )
        _log_result("aggregate_column", result=out,
                    extra={"reason": "no_gateway_bound"})
        return out

    real_table = _resolve_real_table(table_name)
    rows = _gateway.query(real_table, filters=None)
    if rows is None:
        out = f"Data unavailable: table '{table_name}' not found for current case."
        _log_result("aggregate_column", result=out,
                    extra={"table_name": table_name, "found": False})
        return out

    total_rows = len(rows)
    filter_descr = ""
    if filter_column and filter_value:
        resolved = _resolve_real_column(rows, filter_column, real_table)
        rows = _apply_filter(rows, resolved, str(filter_value), filter_op)
        filter_descr = (
            f" filtered by {resolved} {filter_op} {filter_value!r}"
            if resolved == filter_column
            else f" filtered by {resolved} (resolved from '{filter_column}') "
                 f"{filter_op} {filter_value!r}"
        )

    n_matching = len(rows)

    # `count` doesn't need the column to be numeric.
    if op == "count":
        result_str = _format_aggregate(n_matching, column, op)
        out = (
            f"count{filter_descr} = {result_str} "
            f"(out of {total_rows:,} total rows in {real_table})"
        )
        _log_result("aggregate_column", result=out,
                    extra={"op": op, "n_matching": n_matching, "total": total_rows})
        return out

    if not rows:
        out = (
            f"{op}({column}){filter_descr} = (no matching rows; "
            f"{total_rows:,} total in {real_table})"
        )
        _log_result("aggregate_column", result=out,
                    extra={"op": op, "n_matching": 0})
        return out

    real_col = _resolve_real_column(rows, column, real_table)
    values: list[float] = []
    skipped = 0
    for r in rows:
        v = r.get(real_col)
        if v is None or v == "":
            skipped += 1
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            skipped += 1

    if not values:
        out = (
            f"No numeric values for column {real_col!r} in "
            f"{n_matching:,} matching row(s). Check column name + dtype."
        )
        _log_result("aggregate_column", result=out,
                    extra={"op": op, "n_matching": n_matching, "skipped": skipped})
        return out

    if op == "sum":
        result = sum(values)
    elif op == "mean" or op == "avg":
        result = sum(values) / len(values)
    elif op == "max":
        result = max(values)
    elif op == "min":
        result = min(values)
    else:
        out = (
            f"Unknown aggregation op {op!r}. Supported: "
            f"sum, mean, max, min, count."
        )
        _log_result("aggregate_column", result=out, extra={"op": op})
        return out

    formatted = _format_aggregate(result, column, op)
    nonnull = len(values)
    out = (
        f"{op}({real_col}){filter_descr} = {formatted} "
        f"(over {nonnull:,} non-null value(s) in {n_matching:,} matching row(s); "
        f"{total_rows:,} total in {real_table})"
    )
    _log_result(
        "aggregate_column", result=out,
        extra={
            "op": op, "column": real_col, "raw_value": result,
            "n_matching": n_matching, "n_nonnull": nonnull,
            "skipped": skipped, "total": total_rows,
        },
    )
    return out


@function_tool
def aggregate_column(
    table_name: str,
    column: str,
    op: str = "sum",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    """Compute an aggregate (sum / mean / max / min / count) over a column.

    Use this for ANY question asking for a total, average, maximum, minimum,
    or count. The result is formatted with thousand separators (e.g.
    '$174,897.36') so it survives the boundary redaction layer that masks
    long digit runs — large aggregates you compute mentally from query_table
    rows would otherwise come back as '***MASKED***'.

    Returns a one-line human-readable string like::
        sum(balance) filtered by Card Portfolio eq 'SBS' = $174,897.36
        (over 1 non-null value in 1 matching row; 3 total in crossbu_cards_data)

    Args:
        table_name: the table to aggregate over (canonical or real name).
        column: the column to aggregate. Must be numeric for sum/mean/max/min.
            Ignored for op='count'.
        op: one of 'sum', 'mean', 'max', 'min', 'count'. Default 'sum'.
        filter_column / filter_value / filter_op: optional row filter, same
            semantics as query_table. When omitted, aggregates over ALL
            rows of the table for the active case.
    """
    return _aggregate_column_impl(
        table_name=table_name,
        column=column,
        op=op,
        filter_column=filter_column,
        filter_value=filter_value,
        filter_op=filter_op,
    )
