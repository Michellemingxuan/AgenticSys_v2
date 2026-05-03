"""End-to-end integration test for DataManagerAgent.sync_catalog."""

from __future__ import annotations

import pytest
import yaml


@pytest.fixture
def sync_env(tmp_path):
    """Build a tmp case folder + profile directory mix, return a live agent."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "transactions.yaml").write_text("""\
table: transactions
description: "Transaction data"
columns:
  amount:
    dtype: float
    description: "Transaction amount"
    aliases: [trans_amt]
  transaction_date:
    dtype: date
    description: "Transaction date"
    aliases: []
""")

    case_root = tmp_path / "data_tables"
    case_root.mkdir()
    case_dir = case_root / "case_A"
    case_dir.mkdir()
    (case_dir / "transactions.csv").write_text(
        "trans_amt,transaction_dt,mystery_field\n"
        "12.50,2025-01-01,foo\n"
        "30.00,2025-02-15,bar\n"
    )
    (case_dir / "brand_new_table.csv").write_text(
        "col_one,col_two\n1,a\n2,b\n"
    )

    from datalayer.catalog import DataCatalog
    from datalayer.gateway import LocalDataGateway
    from agent_factories.data_manager_agent import DataManagerAgent

    class _NullLogger:
        def log(self, *args, **kwargs):
            pass

    class _NullLLM:
        pass

    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(case_root))

    agent = DataManagerAgent(
        gateway=gateway,
        catalog=catalog,
        llm=_NullLLM(),
        logger=_NullLogger(),
    )
    return agent, profile_dir


def test_sync_catalog_end_to_end(sync_env):
    agent, profile_dir = sync_env
    diff = agent.sync_catalog("case_A")

    auto_cols = {e.real_col for e in diff.auto_aliased}
    assert "trans_amt" in auto_cols

    ambig_cols = {e.real_col for e in diff.ambiguous}
    assert "transaction_dt" in ambig_cols

    new_cols = {e.real_col for e in diff.new}
    assert "mystery_field" in new_cols

    assert "brand_new_table" in diff.new_tables

    with open(profile_dir / "transactions.yaml") as f:
        trans = yaml.safe_load(f)
    assert "mystery_field" in trans["columns"]
    assert trans["columns"]["mystery_field"]["description_pending"] is True
    assert "transaction_dt" not in (
        trans["columns"].get("transaction_date", {}).get("aliases", []) or []
    )

    new_profile = profile_dir / "brand_new_table.yaml"
    assert new_profile.exists()
    with open(new_profile) as f:
        bnt = yaml.safe_load(f)
    assert "col_one" in bnt["columns"]
    assert bnt["columns"]["col_one"]["description_pending"] is True


def test_verify_description_flips_pending(sync_env):
    agent, profile_dir = sync_env
    agent.sync_catalog("case_A")

    agent.verify_description(
        table="transactions",
        column="mystery_field",
        new_text="field of mysterious provenance",
    )

    with open(profile_dir / "transactions.yaml") as f:
        trans = yaml.safe_load(f)
    col = trans["columns"]["mystery_field"]
    assert col["description_pending"] is False
    assert col["description"] == "field of mysterious provenance"


def test_verify_description_without_edit(sync_env):
    """verify_description with no new_text just flips the flag."""
    agent, profile_dir = sync_env
    agent.sync_catalog("case_A")

    with open(profile_dir / "transactions.yaml") as f:
        trans_before = yaml.safe_load(f)
    before_desc = trans_before["columns"]["mystery_field"]["description"]

    agent.verify_description(table="transactions", column="mystery_field")

    with open(profile_dir / "transactions.yaml") as f:
        trans_after = yaml.safe_load(f)
    col = trans_after["columns"]["mystery_field"]
    assert col["description_pending"] is False
    assert col["description"] == before_desc


# ── Drift-detection tests ─────────────────────────────────────────────────


@pytest.fixture
def drift_env(tmp_path):
    """Profile + case where the real CSV's categorical vocabulary disagrees
    with what the canonical profile declares. Mimics the production case
    where ``payments.return_flag`` profile says success/returned but the
    real CSV uses 0/1.
    """
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "payments.yaml").write_text("""\
table: payments
description: "Payment records"
columns:
  return_flag:
    dtype: categorical
    categories:
      success: 0.88
      returned: 0.12
    description: "Payment cleared (success) or returned (returned)"
    aliases: [Return Flag]
  payment_amount:
    dtype: float
    description: "Amount in USD"
    aliases: [Payment Amount]
  signup_date:
    dtype: float
    description: "Wrong dtype on canonical — real CSV has dates"
    aliases: []
""")

    case_root = tmp_path / "data_tables"
    case_root.mkdir()
    case_dir = case_root / "case_drift"
    case_dir.mkdir()
    # Real CSV: Title-Case Return Flag with binary 0/1, plus date column
    # whose canonical was wrongly typed as float.
    (case_dir / "payments.csv").write_text(
        "Return Flag,Payment Amount,signup_date\n"
        "0,100.00,2025-01-15\n"
        "0,250.00,2025-02-20\n"
        "1,75.00,2025-03-10\n"
        "0,500.00,2025-04-05\n"
    )

    from datalayer.catalog import DataCatalog
    from datalayer.gateway import LocalDataGateway
    from agent_factories.data_manager_agent import DataManagerAgent

    class _NullLogger:
        def log(self, *args, **kwargs):
            pass

    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(case_root))
    agent = DataManagerAgent(
        gateway=gateway, catalog=catalog, llm=None, logger=_NullLogger(),
    )
    return agent, profile_dir


def test_sync_detects_categories_drift_and_replaces_vocabulary(drift_env):
    """When real CSV values are disjoint from canonical categories, sync
    should replace (not merge) the categories dict and flag for review."""
    agent, profile_dir = drift_env
    diff = agent.sync_catalog("case_drift")

    # The Return Flag column auto-aliases AND has categories drift.
    drifted = {(d.real_table, d.real_col) for d in diff.value_drift}
    assert ("payments", "Return Flag") in drifted

    rf_entry = next(
        d for d in diff.value_drift if d.real_col == "Return Flag"
    )
    assert rf_entry.categories_drift is True
    assert rf_entry.observed_categories is not None
    # 4 rows: three 0's, one 1 → frequencies 0.75 / 0.25
    assert set(rf_entry.observed_categories.keys()) == {"0", "1"}

    # YAML was overwritten — old success/returned vocabulary is GONE.
    with open(profile_dir / "payments.yaml") as f:
        prof = yaml.safe_load(f)
    cats = prof["columns"]["return_flag"]["categories"]
    assert set(cats.keys()) == {"0", "1"}
    assert "success" not in cats
    assert "returned" not in cats
    assert prof["columns"]["return_flag"].get("categories_pending_review") is True


def test_sync_detects_dtype_drift_and_overwrites(drift_env):
    """When canonical declares the wrong dtype (float for a date column),
    sync should overwrite to the observed dtype and flag for review."""
    agent, profile_dir = drift_env
    diff = agent.sync_catalog("case_drift")

    drifted_cols = {d.real_col for d in diff.value_drift}
    assert "signup_date" in drifted_cols
    sd_entry = next(d for d in diff.value_drift if d.real_col == "signup_date")
    assert sd_entry.dtype_drift is True

    with open(profile_dir / "payments.yaml") as f:
        prof = yaml.safe_load(f)
    assert prof["columns"]["signup_date"]["dtype"] == "date"
    assert prof["columns"]["signup_date"].get("dtype_pending_review") is True


def test_sync_skips_categories_writeback_when_pii_suspected(tmp_path):
    """When observed categorical values look like raw PII (6+ digit runs),
    sync flags the drift but does NOT persist the new vocabulary — keeping
    unmasked card / account numbers out of the catalog YAML (which is
    source-controlled).
    """
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "payments.yaml").write_text("""\
table: payments
description: "Payments"
columns:
  card_number:
    dtype: categorical
    categories:
      '****1234': 0.5
      '****5678': 0.5
    description: "Masked card number"
    aliases: [Card Number]
""")
    case_root = tmp_path / "data_tables"
    case_root.mkdir()
    case_dir = case_root / "case_pii"
    case_dir.mkdir()
    # Real CSV has UNMASKED card numbers — drift, but PII-suspect.
    (case_dir / "payments.csv").write_text(
        "Card Number\n37675218257\n37675218257\n12345678901\n"
    )

    from datalayer.catalog import DataCatalog
    from datalayer.gateway import LocalDataGateway
    from agent_factories.data_manager_agent import DataManagerAgent

    class _NullLogger:
        def log(self, *args, **kwargs):
            pass

    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(case_root))
    agent = DataManagerAgent(
        gateway=gateway, catalog=catalog, llm=None, logger=_NullLogger(),
    )

    diff = agent.sync_catalog("case_pii")

    # Drift is detected and surfaced for human review.
    assert any(d.real_col == "Card Number" for d in diff.value_drift)

    # But the YAML's categories dict is NOT replaced with raw PII.
    with open(profile_dir / "payments.yaml") as f:
        prof = yaml.safe_load(f)
    cats = prof["columns"]["card_number"]["categories"]
    # Original masked vocabulary preserved; no raw PAN leaked.
    assert "****1234" in cats
    assert "37675218257" not in cats
    # The skip is recorded for visibility.
    assert prof["columns"]["card_number"].get("categories_writeback_skipped") == "pii_suspected"


def test_sync_no_drift_when_real_subset_of_declared(tmp_path):
    """Partial overlap (observed ⊂ declared) is NOT drift — a single case
    may not exhibit every declared category."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "payments.yaml").write_text("""\
table: payments
description: "Payments"
columns:
  status:
    dtype: categorical
    categories:
      success: 0.7
      pending: 0.2
      failed: 0.1
    description: "Payment status"
    aliases: []
""")
    case_root = tmp_path / "data_tables"
    case_root.mkdir()
    case_dir = case_root / "case_subset"
    case_dir.mkdir()
    # Real has only 'success' — strict subset of declared.
    (case_dir / "payments.csv").write_text(
        "status\nsuccess\nsuccess\nsuccess\n"
    )

    from datalayer.catalog import DataCatalog
    from datalayer.gateway import LocalDataGateway
    from agent_factories.data_manager_agent import DataManagerAgent

    class _NullLogger:
        def log(self, *args, **kwargs):
            pass

    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(case_root))
    agent = DataManagerAgent(
        gateway=gateway, catalog=catalog, llm=None, logger=_NullLogger(),
    )

    diff = agent.sync_catalog("case_subset")
    assert all(d.real_col != "status" for d in diff.value_drift)

    with open(profile_dir / "payments.yaml") as f:
        prof = yaml.safe_load(f)
    assert set(prof["columns"]["status"]["categories"].keys()) == {
        "success", "pending", "failed",
    }
