"""Pillar-level access control for data tables and columns."""

from __future__ import annotations

from typing import Any


class PillarAccessControl:
    """Controls which tables and columns are visible for a given pillar."""

    def __init__(
        self,
        silenced_tables: set[str] | None = None,
        silenced_columns: dict[str, set[str]] | None = None,
    ):
        self.silenced_tables: set[str] = silenced_tables or set()
        self.silenced_columns: dict[str, set[str]] = silenced_columns or {}

    def is_table_allowed(self, table: str) -> bool:
        return table not in self.silenced_tables

    def filter_row(self, table: str, row: dict[str, Any]) -> dict[str, Any] | None:
        if not self.is_table_allowed(table):
            return None
        hidden = self.silenced_columns.get(table, set())
        if not hidden:
            return row
        return {k: v for k, v in row.items() if k not in hidden}
