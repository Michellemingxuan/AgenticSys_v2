"""Score drivers carry the VALUE of each driver feature, not just its name.

`score_drivers*` stores a driver as a feature NAME (`top_cdss1 =
"last_cycle_cut_revolve_rate"`); the value lives in the modeling table. A case
reviewer reading "top CDSS driver: last_cycle_cut_revolve_rate" still has to go
ask what it was — which is the actual question. Two paths:

  * monthly      → `score_driver_values` (joins score_drivers → model_scores)
  * transactional→ `transaction_detail`'s `driver_values` map
"""
import json

import pytest

from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from tools import data_tools


@pytest.fixture
def catalog():
    return DataCatalog(profile_dir="config/data_profiles")


def _init(case_data, case, catalog):
    gw = LocalDataGateway(case_data=case_data)
    gw.set_case(case)
    data_tools.init_tools(gw, catalog)
    return gw


# ── monthly: score_driver_values ────────────────────────────────────────────

_MONTHLY = {
    "CASE-M": {
        "score_drivers": [
            {"trans_month": "2024-01-01", "top_cdss1": "last_cycle_cut_revolve_rate",
             "top_cdss2": "times_30_dpd", "bottom_cdss1": "cbr_score",
             "top_tsr1": "times_30_dpd"},
            {"trans_month": "2024-02-01", "top_cdss1": "times_30_dpd",
             "top_cdss2": "", "bottom_cdss1": "cbr_score",
             "top_tsr1": "last_cycle_cut_revolve_rate"},
        ],
        "model_scores": [
            {"trans_month": "2024-01-01", "last_cycle_cut_revolve_rate": 0.31,
             "times_30_dpd": 2, "cbr_score": 690},
            {"trans_month": "2024-02-01", "last_cycle_cut_revolve_rate": 0.28,
             "times_30_dpd": 3, "cbr_score": 675},
        ],
    },
}


def test_each_driver_carries_its_value_for_that_month(catalog):
    _init(_MONTHLY, "CASE-M", catalog)
    out = json.loads(data_tools._score_driver_values_impl(period="2024-01"))
    top = out["months"][0]["drivers"]["top_cdss"]
    assert top[0] == {"rank": 1, "feature": "last_cycle_cut_revolve_rate",
                      "value": 0.31}
    assert top[1]["value"] == 2                       # times_30_dpd, same month


def test_values_track_the_month_not_the_first_row(catalog):
    """The join must be per-month — a single stale value for every month would
    read as 'the driver never moved', the opposite of the real finding."""
    _init(_MONTHLY, "CASE-M", catalog)
    out = json.loads(data_tools._score_driver_values_impl())
    by_month = {m["trans_month"]: m["drivers"] for m in out["months"]}
    assert by_month["2024-01-01"]["bottom_cdss"][0]["value"] == 690
    assert by_month["2024-02-01"]["bottom_cdss"][0]["value"] == 675


def test_directions_and_families_stay_separate(catalog):
    _init(_MONTHLY, "CASE-M", catalog)
    drivers = json.loads(
        data_tools._score_driver_values_impl(period="2024-01"))["months"][0]["drivers"]
    assert set(drivers) == {"top_cdss", "bottom_cdss", "top_tsr"}
    assert drivers["top_tsr"][0]["feature"] == "times_30_dpd"


def test_score_filter_restricts_the_family(catalog):
    _init(_MONTHLY, "CASE-M", catalog)
    drivers = json.loads(data_tools._score_driver_values_impl(
        period="2024-01", score="tsr"))["months"][0]["drivers"]
    assert set(drivers) == {"top_tsr"}


def test_blank_driver_cells_are_skipped(catalog):
    _init(_MONTHLY, "CASE-M", catalog)
    feb = json.loads(data_tools._score_driver_values_impl(
        period="2024-02"))["months"][0]["drivers"]
    assert [d["rank"] for d in feb["top_cdss"]] == [1]      # top_cdss2 was ""


def test_period_accepts_any_format_the_data_uses(catalog):
    """Real exports write `July'2023`; the filter must not require ISO."""
    case = {"CASE-F": {
        "score_drivers": [{"trans_month": "July'2023", "top_cdss1": "times_30_dpd"}],
        "model_scores": [{"trans_month": "July'2023", "times_30_dpd": 4}],
    }}
    _init(case, "CASE-F", catalog)
    for period in ("July'2023", "2023-07", "2023-07-01"):
        out = json.loads(data_tools._score_driver_values_impl(period=period))
        assert out["months"][0]["drivers"]["top_cdss"][0]["value"] == 4, period


def test_unparseable_period_is_reported_not_silently_empty(catalog):
    _init(_MONTHLY, "CASE-M", catalog)
    out = data_tools._score_driver_values_impl(period="last quarter")
    assert "could not parse period" in out


def test_feature_absent_from_the_modeling_export_is_counted_not_faked(catalog):
    case = {"CASE-X": {
        "score_drivers": [{"trans_month": "2024-01-01", "top_cdss1": "not_exported"}],
        "model_scores": [{"trans_month": "2024-01-01", "times_30_dpd": 1}],
    }}
    _init(case, "CASE-X", catalog)
    out = json.loads(data_tools._score_driver_values_impl())
    item = out["months"][0]["drivers"]["top_cdss"][0]
    assert item["feature"] == "not_exported"
    assert "value" not in item                        # no invented number
    assert out["unresolved_driver_values"] == 1


def test_aggregated_export_columns_resolve_and_are_labelled(catalog):
    """The real monthly export ships `<feature>_max` / `_min`, and most are not
    declared as catalog aliases — without the suffix fallback nearly every
    driver comes back name-only."""
    case = {"CASE-A": {
        "score_drivers": [{"trans_month": "2024-01-01", "top_cdss1": "cbr_score"}],
        "model_scores": [{"trans_month": "2024-01-01", "cbr_score_max": 705}],
    }}
    _init(case, "CASE-A", catalog)
    item = json.loads(data_tools._score_driver_values_impl(
    ))["months"][0]["drivers"]["top_cdss"][0]
    assert item["value"] == 705
    assert item["value_column"] == "cbr_score_max"    # aggregation made explicit


def test_missing_driver_table_is_reported(catalog):
    _init({"CASE-N": {"model_scores": [{"trans_month": "2024-01-01"}]}},
          "CASE-N", catalog)
    assert "not found for current case" in data_tools._score_driver_values_impl()


def test_uninitialized_data_layer_is_reported():
    data_tools.init_tools(None, None)
    assert "not initialized" in data_tools._score_driver_values_impl()


# ── transactional: transaction_detail's driver_values ───────────────────────

_TXN = {
    "CASE-T": {
        "spends": [
            {"Timestamp": "2025-05-14 11:35:35.101", "Merchant Name": "S BERTRAM",
             "Amount": 412.0},
        ],
        "model_scores_transaction": [
            {"txn_date_time": "2025-05-14 11:35:35.101",
             "tot_struct_risk_score": 24.1, "credit_loss_prob": 0.07,
             "last_cycle_cut_revolve_rate": 0.31, "times_30_dpd": 2},
        ],
        "score_drivers_transaction": [
            {"txn_date_time": "2025-05-14 11:35:35.101",
             "top_cdss1": "last_cycle_cut_revolve_rate",
             "top_cdss2": "times_30_dpd",
             "bottom_cdss1": "times_30_dpd",
             "top_tsr1": "last_cycle_cut_revolve_rate"},
        ],
    },
}


def test_transaction_detail_attaches_driver_values(catalog):
    _init(_TXN, "CASE-T", catalog)
    row = json.loads(data_tools._transaction_detail_impl())["rows"][0]
    assert row["driver_values"] == {"last_cycle_cut_revolve_rate": 0.31,
                                    "times_30_dpd": 2}


def test_driver_columns_keep_their_bare_feature_name(catalog):
    """Additive only — callers that match on the driver name must still work."""
    _init(_TXN, "CASE-T", catalog)
    row = json.loads(data_tools._transaction_detail_impl())["rows"][0]
    assert row["top_cdss1"] == "last_cycle_cut_revolve_rate"
    assert row["top_tsr1"] == "last_cycle_cut_revolve_rate"


def test_driver_values_are_deduplicated_across_columns(catalog):
    """CDSS and TSR routinely cite the same feature; one map, not one per column
    — transaction_detail truncates on total characters."""
    _init(_TXN, "CASE-T", catalog)
    row = json.loads(data_tools._transaction_detail_impl())["rows"][0]
    assert len(row["driver_values"]) == 2             # 4 driver cols, 2 features


def test_no_driver_values_key_when_drivers_dont_join(catalog):
    case = {"CASE-U": {"spends": [{"Timestamp": "2025-05-14 11:35:35.101",
                                   "Merchant Name": "X", "Amount": 1.0}]}}
    _init(case, "CASE-U", catalog)
    row = json.loads(data_tools._transaction_detail_impl())["rows"][0]
    assert "driver_values" not in row
