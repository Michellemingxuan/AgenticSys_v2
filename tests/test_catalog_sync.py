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
    from agents.data_manager_agent import DataManagerAgent

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
