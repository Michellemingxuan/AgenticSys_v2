"""Data gateway ABC and simulated implementation.

Data model: each case (identified by case_id) is associated with a set of
data tables. In the deployment environment, each case maps to a folder
containing table CSVs. The gateway abstracts this — callers query by table
name and the gateway returns data scoped to the current case.
"""

from __future__ import annotations

import csv
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class DataGateway(ABC):
    """Abstract data gateway. All queries are scoped to a case_id."""

    @abstractmethod
    def set_case(self, case_id: str) -> None:
        """Set the active case. All subsequent queries are scoped to this case."""
        ...

    @abstractmethod
    def get_case_id(self) -> str | None:
        """Return the currently active case_id.

        WARNING: The return value MUST NOT be included in any LLM-bound string
        (tool result, prompt, error message). Use ``_display_path()`` or the
        ``<case>`` literal when composing LLM-bound content.
        """
        ...

    def _display_path(self, table: str) -> str:
        """Render a path for user/LLM-facing messages without leaking the raw case ID.

        Real filesystem paths stay internal; any string that can flow back to a caller,
        tool result, or LLM prompt should use this helper instead.
        """
        return f"<case>/{table}.csv"

    @abstractmethod
    def list_case_ids(self) -> list[str]:
        """List all available case IDs."""
        ...

    @abstractmethod
    def query(
        self, table: str, filters: dict[str, Any] | None = None,
    ) -> list[dict] | None:
        """Query a table for the current case. Returns None if table doesn't exist."""
        ...

    @abstractmethod
    def list_tables(self) -> list[str]:
        """List tables available for the current case."""
        ...


class SimulatedDataGateway(DataGateway):
    """In-memory gateway backed by per-case table data.

    Data structure: {case_id: {table_name: [row_dicts]}}
    """

    def __init__(self, case_data: dict[str, dict[str, list[dict]]] | None = None):
        self._case_data: dict[str, dict[str, list[dict]]] = case_data or {}
        self._current_case: str | None = None

    def set_case(self, case_id: str) -> None:
        self._current_case = case_id

    def get_case_id(self) -> str | None:
        return self._current_case

    def list_case_ids(self) -> list[str]:
        return sorted(self._case_data.keys())

    def query(
        self, table: str, filters: dict[str, Any] | None = None,
    ) -> list[dict] | None:
        if self._current_case is None:
            return None
        case_tables = self._case_data.get(self._current_case)
        if case_tables is None:
            return None
        rows = case_tables.get(table)
        if rows is None:
            return None

        if filters:
            rows = [
                r for r in rows
                if all(str(r.get(k, "")) == str(v) for k, v in filters.items())
            ]

        return rows

    def list_tables(self) -> list[str]:
        if self._current_case is None:
            # Return all known tables across all cases
            all_tables: set[str] = set()
            for tables in self._case_data.values():
                all_tables.update(tables.keys())
            return sorted(all_tables)
        case_tables = self._case_data.get(self._current_case, {})
        return sorted(case_tables.keys())

    @classmethod
    def from_generated(cls, tables_raw: dict[str, dict[str, list]]) -> "SimulatedDataGateway":
        """Build per-case data from generator's column-oriented output.

        The generator produces {table_name: {col_name: [values]}}.
        This method pivots it into {case_id: {table_name: [row_dicts]}}.
        """
        case_data: dict[str, dict[str, list[dict]]] = {}

        for table_name, cols in tables_raw.items():
            col_names = list(cols.keys())
            n = len(next(iter(cols.values())))

            if "case_id" not in cols:
                continue

            for i in range(n):
                case_id = cols["case_id"][i]
                # Build row dict without case_id (it's implicit from the case context)
                row = {c: cols[c][i] for c in col_names if c != "case_id"}

                if case_id not in case_data:
                    case_data[case_id] = {}
                if table_name not in case_data[case_id]:
                    case_data[case_id][table_name] = []
                case_data[case_id][table_name].append(row)

        return cls(case_data=case_data)

    @classmethod
    def from_case_folders(cls, data_dir: str) -> "SimulatedDataGateway":
        """Load per-case data from folder structure: data_dir/{case_id}/{table}.csv."""
        case_data: dict[str, dict[str, list[dict]]] = {}
        data_path = Path(data_dir)

        if not data_path.is_dir():
            return cls(case_data={})

        for case_dir in sorted(data_path.iterdir()):
            if not case_dir.is_dir():
                continue
            case_id = case_dir.name
            case_data[case_id] = {}

            for csv_file in sorted(case_dir.glob("*.csv")):
                table_name = csv_file.stem
                with open(csv_file) as f:
                    reader = csv.DictReader(f)
                    case_data[case_id][table_name] = list(reader)

        return cls(case_data=case_data)
