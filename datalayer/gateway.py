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



# Invisible characters that can pad a case id and that `str.strip()` does
# NOT remove — it only strips characters where `isspace()` is true, which
# covers ordinary spaces, tabs and NBSP but not the zero-width family or a
# stray BOM. Ids pasted out of Excel, a ticket, or a browser carry these.
# Spelled as code points rather than literals on purpose: these characters
# are invisible, so a literal string would be an unreviewable blank in the
# diff and a stray edit could silently change the set.
_CASE_ID_INVISIBLE = "".join(chr(c) for c in (
    0x200B,  # zero-width space
    0x200C,  # zero-width non-joiner
    0x200D,  # zero-width joiner
    0x2060,  # word joiner
    0xFEFF,  # BOM / zero-width no-break space
))


def normalize_case_id(raw: Any) -> str:
    """Canonical form of a case id. Apply at EVERY ingress.

    A case id enters the system through three doors, none of which promises
    a clean string:

      1. a directory name under ``data_tables/<source>/``;
      2. a ``case_id`` column in a long-format CSV;
      3. the ``<case_id>`` path segment of an HTTP route.

    Real exports deliver padded values — ``data_tables/real/`` currently
    holds a directory named literally ``"11854808010 "`` — and the id is not
    just a lookup key: it BUILDS PATHS. ``reports/<case_id>/charts/``,
    ``logs/case-<case_id>-<run>.jsonl``. So one case arriving through two
    doors with two spellings forks into two report directories and two log
    files, and a chart written under one spelling 404s when requested under
    the other. Normalizing at ingress is what keeps the id a single value.

    LENGTH IS NOT NORMALIZED, deliberately. Ids vary in length (11854808010
    is 11 digits, 366132845011 is 12) and carry no fixed width or check
    digit to validate against, so zero-padding, truncating, or rejecting on
    length would corrupt real ids. Only invisible padding is removed; every
    visible character is preserved exactly, including case and punctuation.

    Non-string input (an int id from a CSV parser) is coerced to ``str`` so
    callers get one type back regardless of source.
    """
    if raw is None:
        return ""
    # `.strip()` first (whitespace incl. NBSP), then the zero-width set it
    # leaves behind, then once more in case stripping a zero-width char
    # exposed trailing whitespace underneath it.
    return str(raw).strip().strip(_CASE_ID_INVISIBLE).strip()


def _strip_row(row: dict) -> dict:
    """Trim padding from CSV cell values at the LOAD boundary.

    The real exports pad string columns to fixed width — 8,587 of 8,888
    `Merchant Name` cells on case 366132845011 carry trailing spaces. Filters
    already tolerated it (`_apply_filter` compares stripped), but the padding
    survived into OUTPUT: group labels, chart axes, quoted merchant names in
    findings, and KB claims all rendered ragged, and two spellings of the same
    merchant could not be compared as strings.

    Fixed here rather than in each tool: one boundary, and every consumer —
    filters, grouping, joins, labels — sees the same clean value. Keys are
    trimmed too, since a padded HEADER would break column resolution outright.
    """
    out = {}
    for k, v in row.items():
        key = k.strip() if isinstance(k, str) else k
        out[key] = v.strip() if isinstance(v, str) else v
    return out


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


class LocalDataGateway(DataGateway):
    """In-memory gateway backed by per-case table data.

    Data structure: {case_id: {table_name: [row_dicts]}}

    Loads from either the DataGenerator (synthetic cases) via
    :meth:`from_generated`, or from a folder of per-case CSV exports
    (real or synthetic-frozen) via :meth:`from_case_folders`.
    """

    def __init__(self, case_data: dict[str, dict[str, list[dict]]] | None = None):
        self._case_data: dict[str, dict[str, list[dict]]] = case_data or {}
        self._current_case: str | None = None

    def set_case(self, case_id: str) -> None:
        # Normalized here rather than at each call site: this is the ONLY
        # place `_current_case` is assigned (`for_case` routes through it),
        # so a caller holding a padded id — an old bookmark, a hand-typed
        # folder name — still resolves to the loaded case instead of
        # silently querying a case that does not exist.
        self._current_case = normalize_case_id(case_id)

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
    def from_generated(cls, tables_raw: dict[str, dict[str, list]]) -> "LocalDataGateway":
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
                case_id = normalize_case_id(cols["case_id"][i])
                # Build row dict without case_id (it's implicit from the case context)
                row = {c: cols[c][i] for c in col_names if c != "case_id"}

                if case_id not in case_data:
                    case_data[case_id] = {}
                if table_name not in case_data[case_id]:
                    case_data[case_id][table_name] = []
                case_data[case_id][table_name].append(row)

        return cls(case_data=case_data)

    @classmethod
    def from_case_folders(cls, data_dir: str) -> "LocalDataGateway":
        """Load per-case data from folder structure: data_dir/{case_id}/{table}.csv.

        Post-load hook: when both ``payments_success.csv`` and ``payments_returns.csv``
        are present for a case, they are rbound into a single ``payments`` table
        with a synthetic ``payment_status`` column (``"success"`` / ``"return"``).
        The two source tables are removed from the case's table set after the
        merge so consumers see a single ``payments`` table aligned with the
        canonical ``payments`` profile.
        """
        case_data: dict[str, dict[str, list[dict]]] = {}
        data_path = Path(data_dir)

        if not data_path.is_dir():
            return cls(case_data={})

        for case_dir in sorted(data_path.iterdir()):
            if not case_dir.is_dir():
                continue
            # The DIRECTORY NAME is the case id — and it is the id everything
            # downstream keys on, so it is normalized right here rather than
            # trusted. `data_tables/real/` currently holds a folder named
            # "11854808010 " (trailing space); without this the padded id
            # propagated into `reports/`, log filenames and the SSE session
            # key. `setdefault`, not `= {}`, so two spellings of one case
            # merge instead of the later one erasing the earlier.
            case_id = normalize_case_id(case_dir.name)
            if not case_id:
                continue
            tables = case_data.setdefault(case_id, {})

            for csv_file in sorted(case_dir.glob("*.csv")):
                table_name = csv_file.stem
                if table_name in tables:
                    # Two directories normalized to the same id and both
                    # define this table. Concatenating would double-count
                    # rows and overwriting would hide a real mistake in the
                    # data directory — so keep the first and name the folder
                    # that was skipped.
                    print(f"[gateway] case {case_id!r}: table {table_name!r} "
                          f"already loaded; ignoring the copy in "
                          f"{case_dir.name!r}. Deduplicate the data folder.")
                    continue
                # utf-8-sig auto-strips a leading BOM, which Excel exports
                # often add and which would otherwise corrupt the first
                # column header (e.g. "﻿customer_name").
                with open(csv_file, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    tables[table_name] = [_strip_row(row) for row in reader]

            cls._rbind_payments(tables)

        return cls(case_data=case_data)

    @staticmethod
    def _rbind_payments(tables: dict[str, list[dict]]) -> None:
        """Merge payments_success + payments_returns into a single 'payments' table
        with a synthetic 'payment_status' discriminator. In-place mutation.

        ``payment_status`` ('success' / 'return') is the canonical discriminator
        the specialists use. ``return_flag`` is the raw 0/1 encoding from the
        source CSV — it carries the same information in a less-readable form
        and was a known confusion source for the LLM (two columns, same fact,
        different value vocabularies). Drop it here so the unified ``payments``
        table exposes exactly one payment-status column.
        """
        succ = tables.pop("payments_success", None)
        retn = tables.pop("payments_returns", None)
        if succ is None and retn is None:
            return

        # Strip raw return-flag encodings before merging — keep payment_status
        # as the single source of truth. Names checked: lowercase canonical
        # plus the "Return Flag" alias declared in payments.yaml.
        _RAW_FLAG_KEYS = ("return_flag", "Return Flag")

        def _drop_raw_flag(row: dict) -> dict:
            return {k: v for k, v in row.items() if k not in _RAW_FLAG_KEYS}

        merged: list[dict] = []
        for row in (succ or []):
            merged.append({**_drop_raw_flag(row), "payment_status": "success"})
        for row in (retn or []):
            merged.append({**_drop_raw_flag(row), "payment_status": "return"})
        if merged:
            tables["payments"] = merged


# Backwards-compat alias — `SimulatedDataGateway` is the old name of the class
# that handles both simulated and real local CSV flavors. Kept for one cycle so
# external imports don't break; remove in a follow-up after internal call sites
# migrate (done here) and external consumers update.
SimulatedDataGateway = LocalDataGateway
