"""Tests for data.adapter — the sync-time schema reconciler."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from data import adapter


REPO_ROOT = Path(__file__).parent.parent
SCOPE_GUARDED_PATHS = [
    "data/gateway.py",
    "data/catalog.py",
    "agents",
    "tools",
]


def test_adapter_module_importable():
    """Smoke test: module imports and constants are defined."""
    assert adapter.FUZZY_THRESHOLD == 0.85
    assert adapter.TOP_K == 3
    assert adapter.DTYPE_COMPAT_THRESHOLD == 0.5


def test_pandas_scope():
    """pandas must ONLY be imported inside data/adapter.py — never by gateway,
    catalog, agents, or tools. Enforced via grep over the guarded paths.
    """
    for rel in SCOPE_GUARDED_PATHS:
        target = REPO_ROOT / rel
        cmd = ["grep", "-rn", "--include=*.py", "import pandas", str(target)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        # grep exit 1 = no matches (good); exit 0 = matches found (fail).
        assert result.returncode == 1, (
            f"pandas import leaked into {rel}:\n{result.stdout}"
        )


# ── Task 2: _normalize_name ────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("fico_score", "ficoscore"),
    ("FICO_Score", "ficoscore"),
    ("trans-amt", "transamt"),
    ("Trans.Amt", "transamt"),
    ("amount_v2", "amountv"),       # non-alnums stripped, trailing digits trimmed
    ("col_123", "col"),             # trailing digits stripped (cleanly numeric tail)
    ("", ""),
    ("a", "a"),
    ("already_normalized_no_change", "alreadynormalizednochange"),
])
def test_normalize_name(raw, expected):
    assert adapter._normalize_name(raw) == expected


def test_normalize_name_idempotent():
    """Applying normalize twice yields the same result as applying once."""
    for raw in ["fico_score", "TRANS-AMT", "amount_v2", ""]:
        once = adapter._normalize_name(raw)
        twice = adapter._normalize_name(once)
        assert once == twice


# ── Task 3: _dtype_compatible ──────────────────────────────────────────────

@pytest.mark.parametrize("samples,canonical_dtype,expected", [
    # Integer-like strings → compatible with int
    (["1", "2", "3"], "int", True),
    (["1", "two", "three"], "int", False),  # 1/3 parse rate < 0.5
    # Float-like → compatible with float
    (["1.5", "2.0", "3.14"], "float", True),
    (["abc", "def"], "float", False),
    # Date-like strings → compatible with date
    (["2025-01-01", "2025-02-15", "2025-12-31"], "date", True),
    (["Nov'2025", "Dec'2025", "Jan'2026"], "date", True),
    (["not a date", "also not"], "date", False),
    # String canonical accepts anything
    (["anything", "goes"], "str", True),
    (["123", "456"], "string", True),
    # Empty samples → treated as compatible (no evidence against)
    ([], "int", True),
    ([None, None], "int", True),
])
def test_dtype_compatible(samples, canonical_dtype, expected):
    assert adapter._dtype_compatible(samples, canonical_dtype) is expected
