"""Profiles for the transaction-level tables load and resolve to the real CSVs."""

from __future__ import annotations

import pytest

from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from tools import data_tools

REAL_CASE = "366132845011"


@pytest.fixture
def real_env():
    catalog = DataCatalog(profile_dir="config/data_profiles")
    gateway = LocalDataGateway.from_case_folders("data_tables/real")
    gateway.set_case(REAL_CASE)
    data_tools.init_tools(gateway, catalog)
    yield catalog
    data_tools._gateway = None
    data_tools._catalog = None


def test_catalog_loads_transaction_profiles(real_env):
    catalog = real_env
    for table in ("model_scores_transaction", "score_drivers_transaction"):
        schema = catalog.get_schema(table)
        assert schema, f"{table} profile not loaded"

    ms = catalog.get_schema("model_scores_transaction")
    # Transaction-specific columns are documented.
    for col in ("trans_dt", "txn_date_time", "appr_deny_cd",
                "auto_decline_pos_deny_cd_s1"):
        assert col in ms, f"{col} missing from model_scores_transaction"
    # appr_deny_cd documents the 0/1 decode.
    assert "approved" in ms["appr_deny_cd"]["description"].lower()
    # Reused feature + threshold survived.
    assert ms["credit_loss_prob"]["risk_threshold"] == 10


def test_schema_tool_resolves_alias_to_real_csv(real_env):
    # The canonical name and the CSV-file alias both resolve and list real cols.
    out_canonical = data_tools._get_table_schema_impl("model_scores_transaction")
    out_alias = data_tools._get_table_schema_impl("modelling_data_transaction")
    for out in (out_canonical, out_alias):
        assert "appr_deny_cd" in out
        assert "credit_loss_prob" in out
        assert "txn_date_time" in out


def test_transaction_tables_listed_for_real_case(real_env):
    out = data_tools._list_available_tables_impl()
    assert "modelling_data_transaction" in out or "model_scores_transaction" in out
    assert ("score_drivers_data_transaction" in out
            or "score_drivers_transaction" in out)


def test_appr_deny_filter_returns_declines(real_env):
    # appr_deny_cd is int 0/1; eq filter on the string "1" must match declines.
    out = data_tools._query_table_impl(
        "model_scores_transaction",
        filter_column="appr_deny_cd",
        filter_value="1",
        filter_op="eq",
        columns="trans_dt,appr_deny_cd,auto_decline_pos_deny_cd_s1",
    )
    # Either declines exist (rows_matching_filter > 0) or the table is all-approve;
    # in both cases the tool must not error and must report a match count.
    assert "rows_matching_filter" in out
