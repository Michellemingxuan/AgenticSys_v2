"""Tests for datalayer.sync helpers + new adapter aggregation/audit."""

from __future__ import annotations

import pytest

from datalayer import adapter
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway


@pytest.fixture
def two_case_env(tmp_path):
    """Two real cases sharing one table + a profile-only ghost table."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "transactions.yaml").write_text("""\
table: transactions
description: "Transaction data"
columns:
  amount:
    dtype: float
    description: "Amount"
    aliases: [trans_amt]
  transaction_date:
    dtype: date
    description: "Date"
""")
    (profile_dir / "ghost_table.yaml").write_text("""\
table: ghost_table
description: "Profile-only — no real case observes this"
columns:
  ghost_col:
    dtype: float
    description: "ghost"
""")

    real = tmp_path / "real"
    real.mkdir()
    (real / "case_A").mkdir()
    (real / "case_A" / "transactions.csv").write_text(
        "trans_amt,transaction_dt,brand_new\n"
        "12.5,2025-01-01,foo\n30.0,2025-02-01,bar\n"
    )
    (real / "case_A" / "extra_table.csv").write_text(
        "col1,col2\n1,a\n2,b\n"
    )
    (real / "case_B").mkdir()
    (real / "case_B" / "transactions.csv").write_text(
        "trans_amt,transaction_dt,brand_new\n40.0,2025-03-01,baz\n"
    )
    return real, profile_dir


def _observed_for(gateway):
    out: dict[str, set[str]] = {}
    for cid in gateway.list_case_ids():
        gateway.set_case(cid)
        for table in gateway.list_tables():
            rows = gateway.query(table) or []
            if rows:
                out.setdefault(table, set()).update(rows[0].keys())
    return out


def test_aggregate_diffs_dedupes_recurring_columns(two_case_env):
    real, profile_dir = two_case_env
    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(real))
    canonical = {t: catalog._profiles[t]["columns"] for t in catalog.list_tables()}

    diff_a = adapter.reconcile_case(gateway, canonical, "case_A")
    diff_b = adapter.reconcile_case(gateway, canonical, "case_B")

    agg = adapter.aggregate_diffs([diff_a, diff_b])
    assert agg.case_count == 2

    # `brand_new` appears in both cases → exactly one entry post-dedup
    matches = [
        e for e in agg.new_columns
        if (e.real_table, e.real_col) == ("transactions", "brand_new")
    ]
    assert len(matches) == 1
    assert "extra_table" in agg.new_tables


def test_audit_profile_only_flags_ghost_table(two_case_env):
    real, profile_dir = two_case_env
    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(real))

    audit = adapter.audit_profile_only(catalog, _observed_for(gateway))
    assert "ghost_table" in audit.profile_only_tables
    assert "transactions" not in audit.profile_only_tables


def test_audit_profile_only_flags_unmatched_columns(two_case_env):
    real, profile_dir = two_case_env
    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(real))

    audit = adapter.audit_profile_only(catalog, _observed_for(gateway))
    keys = {(e.table, e.column) for e in audit.profile_only_columns}
    # `transaction_date` is canonical with no alias for the real "transaction_dt"
    assert ("transactions", "transaction_date") in keys


def test_dtype_conflict_detection_in_aggregate():
    diff_a = adapter.Diff(case_id="A")
    diff_a.new.append(adapter.ColumnDiff(
        real_table="t", real_col="c", real_dtype="int", bucket="new",
    ))
    diff_b = adapter.Diff(case_id="B")
    diff_b.new.append(adapter.ColumnDiff(
        real_table="t", real_col="c", real_dtype="string", bucket="new",
    ))

    agg = adapter.aggregate_diffs([diff_a, diff_b])
    assert any(
        table == "t" and col == "c" and dtypes == {"int", "string"}
        for table, col, dtypes in agg.dtype_conflicts
    )


def test_audit_handles_table_substring_match(tmp_path):
    """A real table whose name *contains* the canonical name should be
    treated as the same table. e.g. real ``bureau_data`` ↔ canonical ``bureau``.
    """
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "bureau.yaml").write_text("""\
table: bureau
description: "bureau"
columns:
  fico_score:
    dtype: int
    description: "FICO"
    aliases: ["FICO Score"]
""")
    catalog = DataCatalog(profile_dir=str(profile_dir))
    observed = {"bureau_data": {"FICO Score", "month"}}

    audit = adapter.audit_profile_only(catalog, observed)
    # Canonical bureau IS observed (via substring match against bureau_data)
    assert "bureau" not in audit.profile_only_tables
    # fico_score is observed via its alias "FICO Score"
    assert ("bureau", "fico_score") not in {(e.table, e.column) for e in audit.profile_only_columns}
