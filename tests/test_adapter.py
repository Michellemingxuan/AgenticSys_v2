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


# ── Task 4: match_column ───────────────────────────────────────────────────

def _canonical_fixture() -> dict[str, dict[str, dict]]:
    """Minimal canonical catalog fixture for match_column tests."""
    return {
        "transactions": {
            "amount": {
                "dtype": "float",
                "aliases": ["trans_amt"],
            },
            "transaction_date": {
                "dtype": "date",
                "aliases": [],
            },
            "mcc": {
                "dtype": "str",
                "aliases": [],
            },
        },
        "bureau": {
            "fico_score": {
                "dtype": "int",
                "aliases": ["fico"],
            },
        },
    }


def test_match_exact_canonical_name_is_auto():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="bureau",
        real_col="fico_score",
        real_samples=["700", "720", "680"],
        canonical=catalog,
    )
    assert result.bucket == "auto"
    assert result.chosen is not None
    assert result.chosen.canonical_col == "fico_score"


def test_match_known_alias_is_auto():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="bureau",
        real_col="fico",
        real_samples=["700", "720"],
        canonical=catalog,
    )
    assert result.bucket == "auto"
    assert result.chosen.canonical_col == "fico_score"


def test_match_normalized_is_auto_when_dtype_compatible():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="transactions",
        real_col="MCC",  # normalizes to mcc
        real_samples=["5411", "5812"],
        canonical=catalog,
    )
    assert result.bucket == "auto"
    assert result.chosen.canonical_col == "mcc"


def test_match_fuzzy_is_ambiguous():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="transactions",
        real_col="transaction_dt",
        real_samples=["2025-01-01", "2025-02-01"],
        canonical=catalog,
    )
    assert result.bucket == "ambiguous"
    assert any(c.canonical_col == "transaction_date" for c in result.candidates)
    assert len(result.candidates) <= adapter.TOP_K


def test_match_no_candidates_is_new():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="misc",
        real_col="totally_unrelated_field",
        real_samples=["a", "b"],
        canonical=catalog,
    )
    assert result.bucket == "new"
    assert result.candidates == []


def test_ambiguous_candidates_sorted_by_ratio_desc():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="transactions",
        real_col="trans_date",
        real_samples=["2025-01-01"],
        canonical=catalog,
    )
    if len(result.candidates) >= 2:
        ratios = [c.ratio for c in result.candidates]
        assert ratios == sorted(ratios, reverse=True)


# ── Task 5: _infer_parse_hint ──────────────────────────────────────────────

@pytest.mark.parametrize("samples,expected", [
    (["2025-01-01", "2025-02-15", "2025-12-31"], "%Y-%m-%d"),
    (["Nov'2025", "Dec'2025", "Jan'2026"], "%b'%Y"),
    (["2025-01", "2025-02", "2025-12"], "%Y-%m"),
    # Unambiguous d/m/Y — only %d/%m/%Y parses month>12.
    (["15/01/2025", "31/12/2025", "04/03/2026"], "%d/%m/%Y"),
    (["not a date", "also nope"], None),
    ([], None),
])
def test_infer_parse_hint(samples, expected):
    assert adapter._infer_parse_hint(samples) == expected


def test_infer_parse_hint_rejects_numeric():
    # Pure numeric samples should NOT be flagged as dates.
    assert adapter._infer_parse_hint(["123", "456", "789"]) is None


# ── Task 6: reconcile_case ─────────────────────────────────────────────────

def test_reconcile_case_produces_three_buckets():
    """End-to-end reconciliation of one case against a tiny catalog."""
    from data.gateway import SimulatedDataGateway

    case_data = {
        "case_A": {
            "transactions": [
                {"trans_amt": "12.50", "transaction_dt": "2025-01-01", "totally_new_field": "x"},
                {"trans_amt": "30.00", "transaction_dt": "2025-02-15", "totally_new_field": "y"},
            ],
        },
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    gateway.set_case("case_A")

    canonical = _canonical_fixture()

    diff = adapter.reconcile_case(gateway, canonical, "case_A")

    assert diff.case_id == "case_A"
    # trans_amt is a known alias of amount → auto
    assert any(e.real_col == "trans_amt" and e.chosen and e.chosen.canonical_col == "amount"
               for e in diff.auto_aliased)
    # transaction_dt fuzzy-matches transaction_date → ambiguous
    assert any(e.real_col == "transaction_dt" for e in diff.ambiguous)
    # totally_new_field has no match → new
    assert any(e.real_col == "totally_new_field" for e in diff.new)


def test_reconcile_case_flags_unknown_tables():
    """A table not in canonical lands in new_tables; its columns go to new."""
    from data.gateway import SimulatedDataGateway

    case_data = {
        "case_A": {
            "brand_new_table": [{"foo": "1"}, {"foo": "2"}],
        },
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    gateway.set_case("case_A")
    canonical = _canonical_fixture()

    diff = adapter.reconcile_case(gateway, canonical, "case_A")

    assert "brand_new_table" in diff.new_tables
    assert any(c.real_table == "brand_new_table" for c in diff.new)


def test_reconcile_case_drafts_description_for_common_patterns():
    """Columns with obvious-naming patterns get an agent-drafted description."""
    from data.gateway import SimulatedDataGateway

    case_data = {
        "case_A": {
            "new_tbl": [
                {"customer_id": "1", "txn_amount": "10.0", "bogusthing": "x"},
                {"customer_id": "2", "txn_amount": "20.0", "bogusthing": "y"},
            ],
        },
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    gateway.set_case("case_A")
    canonical = _canonical_fixture()

    diff = adapter.reconcile_case(gateway, canonical, "case_A")

    by_name = {e.real_col: e for e in diff.new}
    assert by_name["customer_id"].drafted_description  # non-empty
    assert by_name["txn_amount"].drafted_description   # non-empty
    assert by_name["bogusthing"].drafted_description == ""  # no pattern match
