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


# ── Dtype compatibility (sync-time, pandas-backed) ────────────────────────

import pandas as pd

_STRING_DTYPES = {"str", "string", "text", "category"}
_DATE_DTYPES = {"date", "datetime", "datetime64", "timestamp"}
_INT_DTYPES = {"int", "integer", "int64", "int32"}
_FLOAT_DTYPES = {"float", "float64", "float32", "number", "numeric"}

# Common date formats tried as fallback when mixed-mode parsing underperforms.
# Ordering matters: unambiguous formats first, ambiguous ones last.
_DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m",
    "%b'%Y",
    "%b %Y",
    "%B %Y",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
]


def _dtype_compatible(samples: list, canonical_dtype: str) -> bool:
    """Check if sample values could plausibly be of the canonical dtype.

    Strategy: try parsing with pandas coercion; require parse success rate
    >= DTYPE_COMPAT_THRESHOLD on non-null samples. Strings are always
    compatible (we can't rule them out without semantic knowledge).
    """
    canonical_dtype = canonical_dtype.lower()
    if canonical_dtype in _STRING_DTYPES:
        return True

    # Filter Nones/empties — if nothing left, no evidence to reject.
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return True

    series = pd.Series([str(s) for s in non_null])

    if canonical_dtype in _INT_DTYPES or canonical_dtype in _FLOAT_DTYPES:
        parsed = pd.to_numeric(series, errors="coerce")
        return bool(parsed.notna().mean() >= DTYPE_COMPAT_THRESHOLD)

    if canonical_dtype in _DATE_DTYPES:
        # Start with mixed-mode (handles most ISO / dateutil-parseable forms).
        best_rate = float(
            pd.to_datetime(series, errors="coerce", format="mixed").notna().mean()
        )
        # Fall back to explicit formats for exotic patterns (e.g., "Nov'2025").
        if best_rate < DTYPE_COMPAT_THRESHOLD:
            for fmt in _DATE_FORMATS:
                try:
                    rate = float(
                        pd.to_datetime(series, errors="coerce", format=fmt).notna().mean()
                    )
                except (ValueError, TypeError):
                    continue
                if rate > best_rate:
                    best_rate = rate
                    if best_rate >= DTYPE_COMPAT_THRESHOLD:
                        break
        return bool(best_rate >= DTYPE_COMPAT_THRESHOLD)

    # Unknown canonical dtype — don't reject.
    return True
