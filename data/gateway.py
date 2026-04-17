"""Data gateway ABC and simulated implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DataGateway(ABC):
    @abstractmethod
    def query(
        self, table: str, filters: dict[str, Any] | None = None, limit: int = 100
    ) -> list[dict] | None:
        ...

    @abstractmethod
    def list_tables(self) -> list[str]:
        ...


class SimulatedDataGateway(DataGateway):
    """In-memory gateway backed by a dict of table data."""

    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def query(
        self, table: str, filters: dict[str, Any] | None = None, limit: int = 100
    ) -> list[dict] | None:
        rows = self._tables.get(table)
        if rows is None:
            return None

        if filters:
            rows = [
                r
                for r in rows
                if all(r.get(k) == v for k, v in filters.items())
            ]

        return rows[:limit]

    def list_tables(self) -> list[str]:
        return sorted(self._tables.keys())
