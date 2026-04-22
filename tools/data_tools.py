"""Data-access tool functions for agent tool-calling.

All queries are scoped to the currently active case. The case_id is set on the
gateway at session start — tools don't need to specify it.
"""

from __future__ import annotations

import json
import operator
import re
from typing import Any, Callable

from data.catalog import DataCatalog
from data.gateway import DataGateway

_gateway: DataGateway | None = None
_catalog: DataCatalog | None = None

_MAX_CHARS = 3000

_FILTER_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


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


def init_tools(gateway: DataGateway, catalog: DataCatalog) -> None:
    global _gateway, _catalog
    _gateway = gateway
    _catalog = catalog


def list_available_tables() -> str:
    """List all data tables available for the current case, each with its description."""
    if _catalog is None:
        return "Data unavailable"

    def _render(tables: list[str]) -> str:
        lines: list[str] = []
        for t in tables:
            desc = _catalog.get_description(t) if _catalog else ""
            if desc:
                lines.append(f"- {t}: {desc}")
            else:
                lines.append(f"- {t}")
        return "\n".join(lines)

    if _gateway is not None and _gateway.get_case_id() is not None:
        case_tables = _gateway.list_tables()
        if case_tables:
            return "Tables for the current case:\n" + _render(case_tables)
        return "No tables available for the current case."
    tables = _catalog.list_tables()
    return _render(tables) if tables else "No tables available"


def get_table_schema(table_name: str) -> str:
    """Get the column schema for a specific table."""
    if _catalog is None:
        return "Data unavailable"
    schema = _catalog.get_schema(table_name)
    if schema is None:
        return "Data unavailable"
    return json.dumps(schema, indent=2)


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
    if _gateway is None:
        return "Data unavailable"

    # Fetch ALL rows for this case, then apply the filter in Python so we
    # can support range operators. The gateway itself only knows exact match.
    rows = _gateway.query(table_name, filters=None)
    if rows is None:
        return f"Data unavailable: table '{table_name}' not found for current case."

    if filter_column and filter_value:
        rows = _apply_filter(rows, filter_column, str(filter_value), filter_op)

    if not rows:
        return (
            f"No rows matching filter ({filter_column} {filter_op} "
            f"{filter_value!r}) in '{table_name}'."
        )

    # Column projection — select only requested columns
    if columns:
        requested = [c.strip() for c in columns.split(",") if c.strip()]
        if requested:
            # Project requested columns only.
            rows = [{k: row[k] for k in requested if k in row} for row in rows]
            if not rows or not rows[0]:
                return f"No requested columns {requested} found in '{table_name}'."

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

    return json.dumps(rows, indent=2, default=str)
