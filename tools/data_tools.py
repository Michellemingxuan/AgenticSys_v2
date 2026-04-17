"""Data-access tool functions for agent tool-calling."""

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
    if _catalog is None:
        return "Data unavailable"
    tables = _catalog.list_tables()
    return "\n".join(tables) if tables else "No tables available"


def get_table_schema(table_name: str) -> str:
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
    limit: int = 50,
) -> str:
    if _gateway is None:
        return "Data unavailable"

    filters: dict[str, Any] | None = None
    if filter_column and filter_value:
        filters = {filter_column: filter_value}

    rows = _gateway.query(table_name, filters=filters, limit=limit)
    if rows is None:
        return "Data unavailable"

    text = json.dumps(rows, indent=2)
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n... (truncated)"
    return text
