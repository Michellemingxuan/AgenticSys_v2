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

_gateway: DataGateway | None = None
_catalog: DataCatalog | None = None
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
# "October'2024", "October 2024", "Oct'2024", "Oct 2024"
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,})\s*['\s]\s*(\d{4})$")


def _date_key(value: Any) -> tuple[int, int, int] | None:
    """Parse common date / period string formats into a comparable
    (year, month, day) tuple. Returns None if unparseable.

    Handles formats produced across the data profiles:
      - ``2025-11-16``           → (2025, 11, 16)
      - ``2025-11``              → (2025, 11, 1)
      - ``2025``                 → (2025, 1, 1)
      - ``October'2024`` / ``October 2024`` / ``Oct'2024`` → (2024, 10, 1)
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
        rows = _gateway.query(table_name) or []
        if not rows:
            out = f"Data unavailable: table '{table_name}' not found for current case."
            _log_result("get_table_schema", result=out,
                        extra={"table_name": table_name, "found": False})
            return out

        canonical_tables = _resolve_canonical_tables(table_name)
        # Build a merged column-spec map across all matching canonical tables.
        # Earlier entries win, so a hand-written real-data profile takes
        # precedence over the broader canonical it shares a name with.
        merged_cols: dict[str, dict] = {}
        for ct in canonical_tables:
            for col, spec in (_catalog._profiles.get(ct, {}).get("columns", {}) or {}).items():
                merged_cols.setdefault(col, spec)

        schema: dict[str, dict] = {}
        for real_col in rows[0].keys():
            spec = _find_column_spec(merged_cols, real_col)
            if spec is not None:
                schema[real_col] = {
                    "type": spec.get("dtype", "unknown"),
                    "description": spec.get("description", ""),
                }
            else:
                schema[real_col] = {"type": "unknown", "description": "(not in catalog)"}

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
        out = "Data unavailable"
        _log_result("query_table", result=out)
        return out

    # Fetch ALL rows for this case, then apply the filter in Python so we
    # can support range operators. The gateway itself only knows exact match.
    rows = _gateway.query(table_name, filters=None)
    if rows is None:
        out = f"Data unavailable: table '{table_name}' not found for current case."
        _log_result("query_table", result=out,
                    extra={"table_name": table_name, "found": False})
        return out

    total_before_filter = len(rows)
    if filter_column and filter_value:
        rows = _apply_filter(rows, filter_column, str(filter_value), filter_op)
    rows_after_filter = len(rows)

    if not rows:
        out = (
            f"No rows matching filter ({filter_column} {filter_op} "
            f"{filter_value!r}) in '{table_name}'."
        )
        _log_result(
            "query_table", result=out, rows_returned=0,
            extra={
                "table_name": table_name,
                "rows_before_filter": total_before_filter,
                "rows_after_filter": 0,
            },
        )
        return out

    # Column projection — select only requested columns
    if columns:
        requested = [c.strip() for c in columns.split(",") if c.strip()]
        if requested:
            # Project requested columns only.
            rows = [{k: row[k] for k in requested if k in row} for row in rows]
            if not rows or not rows[0]:
                out = f"No requested columns {requested} found in '{table_name}'."
                _log_result(
                    "query_table", result=out, rows_returned=0,
                    extra={
                        "table_name": table_name,
                        "rows_before_filter": total_before_filter,
                        "rows_after_filter": rows_after_filter,
                        "reason": "no_requested_columns_present",
                    },
                )
                return out

    total_rows = len(rows)
    total_cols = len(rows[0]) if rows else 0
    truncation_notes: list[str] = []

    # Step 1: trim columns if a single row is already too wide
    if total_cols > 0:
        single_row_size = len(json.dumps([rows[0]], indent=2, default=str))
        if single_row_size > _MAX_CHARS - 200:
            keys = list(rows[0].keys())
            keep_keys: list[str] = []
            for k in keys:
                test_row = {kk: rows[0][kk] for kk in keep_keys + [k]}
                if len(json.dumps([test_row], indent=2, default=str)) > _MAX_CHARS - 300:
                    break
                keep_keys.append(k)
            rows = [{k: row[k] for k in keep_keys if k in row} for row in rows]
            truncation_notes.append(f"showing {len(keep_keys)}/{total_cols} columns")

    # Step 2: reduce rows until JSON fits
    text = json.dumps(rows, indent=2, default=str)
    shown_rows = len(rows)
    while len(text) > _MAX_CHARS and len(rows) > 1:
        rows = rows[: len(rows) // 2]
        shown_rows = len(rows)
        text = json.dumps(rows, indent=2, default=str)

    if shown_rows < total_rows:
        truncation_notes.append(f"showing {shown_rows}/{total_rows} rows")

    if truncation_notes:
        rows.append({"_truncated": ", ".join(truncation_notes)})

    out = json.dumps(rows, indent=2, default=str)
    _log_result(
        "query_table", result=out, rows_returned=shown_rows,
        extra={
            "table_name": table_name,
            "rows_before_filter": total_before_filter,
            "rows_after_filter": rows_after_filter,
            "rows_shown": shown_rows,
            "total_rows": total_rows,
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
