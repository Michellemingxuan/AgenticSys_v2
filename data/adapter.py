"""Schema reconciliation between real CSV data and the canonical catalog.

This module is invoked only at sync time (explicit trigger), never at query time.
It is the ONLY place in the codebase that imports pandas — the gateway and catalog
stay pure-Python. See tests/test_adapter.py::test_pandas_scope for enforcement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# Tunable thresholds — constants at module top so they're easy to find.
FUZZY_THRESHOLD = 0.85       # minimum difflib ratio to surface as candidate
TOP_K = 3                    # max candidates shown for an ambiguous column
DTYPE_COMPAT_THRESHOLD = 0.5 # min parse-success rate to call dtype "compatible"


@dataclass
class Candidate:
    """One potential canonical match for a real column."""
    canonical_table: str
    canonical_col: str
    ratio: float
    canonical_dtype: str
    dtype_compatible: bool


@dataclass
class ColumnDiff:
    """Result of matching one real column against the catalog."""
    real_table: str
    real_col: str
    real_dtype: str
    bucket: Literal["auto", "ambiguous", "new"]
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None
    parse_hint: str | None = None
    drafted_description: str = ""


@dataclass
class Diff:
    """Full diff for a case — the output of reconcile_case."""
    case_id: str
    auto_aliased: list[ColumnDiff] = field(default_factory=list)
    ambiguous: list[ColumnDiff] = field(default_factory=list)
    new: list[ColumnDiff] = field(default_factory=list)
    new_tables: list[str] = field(default_factory=list)


# ── Name normalization ─────────────────────────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9]")
_TRAILING_DIGITS = re.compile(r"\d+$")


def _normalize_name(name: str) -> str:
    """Normalize a column or table name for fuzzy comparison.

    Lowercase → strip non-alphanumerics → trim trailing digits.
    Idempotent: normalize(normalize(x)) == normalize(x).
    """
    lower = name.lower()
    alnum = _NON_ALNUM.sub("", lower)
    return _TRAILING_DIGITS.sub("", alnum)
