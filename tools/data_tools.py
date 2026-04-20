"""Data-access tool functions for agent tool-calling.

All queries are scoped to the currently active case. The case_id is set on the
gateway at session start — tools don't need to specify it.
"""

from __future__ import annotations

import json
from typing import Any

from data.catalog import DataCatalog
from data.gateway import DataGateway

_gateway: DataGateway | None = None
_catalog: DataCatalog | None = None

_MAX_CHARS = 3000


def init_tools(gateway: DataGateway, catalog: DataCatalog) -> None:
    global _gateway, _catalog
    _gateway = gateway
    _catalog = catalog


def list_available_tables() -> str:
    """List all data tables available for the current case."""
    if _catalog is None:
        return "Data unavailable"
    tables = _catalog.list_tables()
    if _gateway is not None:
        case_id = _gateway.get_case_id()
        if case_id:
            # Show only tables that exist for this case
            case_tables = _gateway.list_tables()
            header = f"Tables for case {case_id}:\n"
            return header + "\n".join(case_tables) if case_tables else header + "No tables available"
    return "\n".join(tables) if tables else "No tables available"


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
    columns: str = "",
) -> str:
    """Query a data table for the current case. All data is scoped to the active case.

    Args:
        table_name: the table to query
        filter_column, filter_value: optional row filter
        columns: comma-separated list of column names to return (e.g. "fico_score,derog_count").
                 Leave empty to return all columns. REQUIRED for wide tables like model_scores
                 (266 cols) to avoid slow processing — request only the columns you need.
    """
    if _gateway is None:
        return "Data unavailable"

    filters: dict[str, Any] | None = None
    if filter_column and filter_value:
        filters = {filter_column: str(filter_value)}

    rows = _gateway.query(table_name, filters=filters)
    if rows is None:
        return f"Data unavailable: table '{table_name}' not found for current case."

    if not rows:
        return f"No rows matching filter in '{table_name}'."

    # Column projection — select only requested columns
    if columns:
        requested = [c.strip() for c in columns.split(",") if c.strip()]
        if requested:
            # Always keep case_id-like identifier columns if present
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
