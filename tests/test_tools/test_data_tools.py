"""Tests for tools/data_tools.py (case-scoped)."""

import pytest

from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from tools import data_tools


@pytest.fixture(autouse=True)
def _setup_tools():
    case_data = {
        "CASE-00001": {
            "bureau_full": [
                {"score": 720, "derog_count": 0},
                {"score": 680, "derog_count": 1},
            ],
        },
        "CASE-00002": {
            "bureau_full": [
                {"score": 580, "derog_count": 4},
            ],
        },
    }
    gateway = LocalDataGateway(case_data=case_data)
    gateway.set_case("CASE-00001")
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)
    yield
    data_tools._gateway = None
    data_tools._catalog = None


def test_list_tables():
    result = data_tools._list_available_tables_impl()
    assert "bureau_full" in result
    assert "Tables for the current case:" in result
    # No raw case ID must leak.
    import re
    assert re.search(r"CASE-\d+", result) is None


def test_list_tables_no_case_set():
    """When no case is active, the tool must fall back to catalog-level listing
    instead of mislabeling all-case tables as 'current case'."""
    case_data = {
        "CASE-00001": {"bureau_full": [{"score": 720}]},
        "CASE-00002": {"bureau_full": [{"score": 580}]},
    }
    gateway = LocalDataGateway(case_data=case_data)
    # Intentionally do NOT call gateway.set_case(...)
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)

    result = data_tools._list_available_tables_impl()

    # The "current case" header must NOT appear when no case is set.
    assert "Tables for the current case:" not in result
    # And absolutely no raw case ID leaks.
    import re
    assert re.search(r"CASE-\d+", result) is None
    # The catalog fallback should list at least one real table.
    assert "bureau" in result

    # Cleanup
    data_tools._gateway = None
    data_tools._catalog = None


def test_get_schema():
    """Case-aware schema returns columns physically present in the case CSV.

    The fixture's case has table ``bureau_full`` whose normalized name
    contains canonical ``bureau`` — the resolver maps it to that profile,
    and the real columns are returned (annotated as ``unknown`` when the
    canonical profile doesn't carry them).
    """
    result = data_tools._get_table_schema_impl("bureau_full")
    assert "score" in result
    assert "derog_count" in result
    # case_id is infrastructure, not schema — must not appear in LLM-bound schema output.
    assert "case_id" not in result
    assert "CASE-" not in result


def test_get_schema_filters_to_case_columns():
    """When a case is active, get_table_schema must NOT return canonical
    columns that the case CSV doesn't actually contain. The fixture case
    only has 'score' + 'derog_count' — sibling canonical columns like
    'fico_score' (a different name in the bureau profile) must NOT appear.
    """
    result = data_tools._get_table_schema_impl("bureau_full")
    # 'fico_score' is in canonical bureau profile but the case CSV uses
    # different column names → must be absent from the case-filtered view
    assert "fico_score" not in result
    assert "delinquent_external_trades" not in result


def test_get_schema_missing():
    result = data_tools._get_table_schema_impl("nonexistent")
    assert "unavailable" in result.lower()


def test_query_all():
    result = data_tools._query_table_impl("bureau_full")
    assert "720" in result
    assert "680" in result


def test_query_filtered():
    result = data_tools._query_table_impl("bureau_full", filter_column="score", filter_value=720)
    assert "720" in result


def test_query_missing():
    result = data_tools._query_table_impl("no_such_table")
    assert "unavailable" in result.lower()


# ── summarize_trend ──────────────────────────────────────────────────────


import json


def _setup_spend_fixture():
    """Five months of mock spend data: rising trend, one missing month, peak Mar."""
    case_data = {
        "CASE-TREND": {
            "spends_data": [
                # Nov-2024: 100 + 200 = 300
                {"Date": "05-Nov-2024", "Amount": 100.0, "Merchant Name": "A"},
                {"Date": "20-Nov-2024", "Amount": 200.0, "Merchant Name": "B"},
                # Dec-2024: 250
                {"Date": "10-Dec-2024", "Amount": 250.0, "Merchant Name": "A"},
                # Jan-2025: skipped (gap)
                # Feb-2025: 400
                {"Date": "14-Feb-2025", "Amount": 400.0, "Merchant Name": "B"},
                # Mar-2025: 500 + 600 = 1100  (peak)
                {"Date": "03-Mar-2025", "Amount": 500.0, "Merchant Name": "A"},
                {"Date": "28-Mar-2025", "Amount": 600.0, "Merchant Name": "C"},
            ],
        },
    }
    gateway = LocalDataGateway(case_data=case_data)
    gateway.set_case("CASE-TREND")
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)


def test_summarize_trend_monthly_sum():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    payload = json.loads(raw)
    assert payload["period"] == "month"
    assert payload["op"] == "sum"
    series = payload["series"]
    assert [s["period"] for s in series] == ["2024-11", "2024-12", "2025-02", "2025-03"]
    assert [s["raw_value"] for s in series] == [300.0, 250.0, 400.0, 1100.0]
    assert payload["summary"]["n_buckets"] == 4
    assert payload["summary"]["n_records"] == 6


def test_summarize_trend_first_last_peak_trough():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    s = json.loads(raw)["summary"]
    assert s["first"]["period"] == "2024-11"
    assert s["last"]["period"] == "2025-03"
    assert s["peak"]["period"] == "2025-03"     # 1100 is the max
    assert s["trough"]["period"] == "2024-12"   # 250 is the min


def test_summarize_trend_detects_missing_month():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    s = json.loads(raw)["summary"]
    # Jan-2025 was skipped in the fixture and falls between first and last.
    assert "2025-01" in s["missing_periods"]


def test_summarize_trend_count_op_uses_one_per_row():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="count",
    )
    series = json.loads(raw)["series"]
    by_period = {s["period"]: s["raw_value"] for s in series}
    assert by_period["2024-11"] == 2
    assert by_period["2025-03"] == 2
    assert by_period["2024-12"] == 1


def test_summarize_trend_quarter_bucketing():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="quarter", op="sum",
    )
    series = json.loads(raw)["series"]
    by_period = {s["period"]: s["raw_value"] for s in series}
    # 2024-Q4 = Nov + Dec = 300 + 250 = 550
    # 2025-Q1 = Feb + Mar = 400 + 1100 = 1500
    assert by_period == {"2024-Q4": 550.0, "2025-Q1": 1500.0}


def test_summarize_trend_slope_rising():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    s = json.loads(raw)["summary"]
    # Series [300, 250, 400, 1100] has positive slope.
    slope_str = s["slope_per_bucket"]
    assert slope_str is not None
    # Strip leading $ and thousand separators to get a positive number.
    val = float(slope_str.lstrip("$").replace(",", ""))
    assert val > 0


def test_summarize_trend_pct_change_first_to_last():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    pct = json.loads(raw)["summary"]["pct_change_first_to_last"]
    # First 300 → last 1100 ≈ +266.7%
    assert pct.endswith("%")
    assert float(pct.rstrip("%")) > 200


def test_summarize_trend_filter_narrows_rows():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
        filter_column="Merchant Name", filter_value="A",
    )
    series = json.loads(raw)["series"]
    by_period = {s["period"]: s["raw_value"] for s in series}
    # Merchant A: Nov 100, Dec 250, Mar 500
    assert by_period == {"2024-11": 100.0, "2024-12": 250.0, "2025-03": 500.0}


def test_summarize_trend_date_range_narrowing():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
        start_date="2025-01-01", end_date="2025-12-31",
    )
    series = json.loads(raw)["series"]
    periods = {s["period"] for s in series}
    # Nov / Dec 2024 must be excluded.
    assert "2024-11" not in periods
    assert "2024-12" not in periods
    assert {"2025-02", "2025-03"} <= periods


def test_summarize_trend_table_alias_resolves():
    _setup_spend_fixture()
    # Pass canonical 'spends' — the spends.yaml profile aliases it to spends_data.
    raw = data_tools._summarize_trend_impl(
        table_name="spends", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    payload = json.loads(raw)
    assert payload["table"] == "spends_data"


def test_summarize_trend_bad_period():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="fortnight", op="sum",
    )
    assert "Unsupported period" in raw


def test_summarize_trend_no_rows():
    _setup_spend_fixture()
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
        filter_column="Merchant Name", filter_value="ZZZ-not-real",
    )
    assert "no rows match" in raw


# ── summarize_by_group ───────────────────────────────────────────────────


def test_summarize_by_group_top_by_sum():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="sum", top_n=5,
    )
    payload = json.loads(raw)
    assert payload["op"] == "sum"
    assert payload["group_column"] == "Merchant Name"
    by_group = {g["group"]: g["raw_value"] for g in payload["groups"]}
    # Fixture totals: A = 100+250+500 = 850, B = 200+400 = 600, C = 600.
    assert by_group["A"] == 850.0
    assert by_group["B"] == 600.0
    assert by_group["C"] == 600.0
    # Default sort = value desc, so "A" should be first.
    assert payload["groups"][0]["group"] == "A"


def test_summarize_by_group_top_by_count_sort_by_count():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="count", top_n=5, sort_by="count",
    )
    payload = json.loads(raw)
    by_group = {g["group"]: g["raw_value"] for g in payload["groups"]}
    # Fixture counts: A = 3, B = 2, C = 1.
    assert by_group["A"] == 3
    assert by_group["B"] == 2
    assert by_group["C"] == 1
    assert payload["groups"][0]["group"] == "A"


def test_summarize_by_group_concentration_block_for_sum():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="sum", top_n=10,
    )
    conc = json.loads(raw)["concentration"]
    assert conc is not None
    # Total = 850 + 600 + 600 = 2050. top1 = A = 850 / 2050 ≈ 41.5%.
    assert conc["top1_share"].endswith("%")
    top1_pct = float(conc["top1_share"].rstrip("%"))
    assert 41.0 <= top1_pct <= 42.0
    # HHI = (850/2050)^2 + (600/2050)^2 + (600/2050)^2 ≈ 0.343
    hhi = float(conc["hhi"])
    assert 0.34 <= hhi <= 0.35


def test_summarize_by_group_concentration_block_omitted_for_mean():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="mean", top_n=5,
    )
    payload = json.loads(raw)
    # Share math is meaningless for non-additive ops.
    assert payload["concentration"] is None


def test_summarize_by_group_top_n_truncates():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="sum", top_n=2,
    )
    payload = json.loads(raw)
    assert payload["n_groups_total"] == 3
    assert payload["n_groups_returned"] == 2
    # Top-2 by sum: A (850), then B/C tied at 600 — stable sort keeps insertion
    # order, so B comes before C. But just check both are NOT C OR contain A.
    groups = [g["group"] for g in payload["groups"]]
    assert groups[0] == "A"
    assert groups[1] in ("B", "C")


def test_summarize_by_group_filter_narrows_rows():
    _setup_spend_fixture()
    # Filter to dates in 2025 only — A has just Mar (500), B has Feb (400),
    # C has Mar (600).
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="sum", top_n=5,
        filter_column="Date", filter_value="2025-01-01,2025-12-31",
        filter_op="between",
    )
    by_group = {g["group"]: g["raw_value"]
                for g in json.loads(raw)["groups"]}
    assert by_group == {"C": 600.0, "B": 400.0, "A": 500.0}


def test_summarize_by_group_table_alias_resolves():
    _setup_spend_fixture()
    # Pass canonical 'spends' — spends.yaml aliases it to spends_data.
    raw = data_tools._summarize_by_group_impl(
        table_name="spends", value_column="Amount",
        group_column="Merchant Name", op="sum", top_n=5,
    )
    assert json.loads(raw)["table"] == "spends_data"


def test_summarize_by_group_per_group_mini_stats():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="sum", top_n=5,
    )
    a = next(g for g in json.loads(raw)["groups"] if g["group"] == "A")
    # Mini stats include mean / max / min for additive ops.
    assert "mean" in a and "max" in a and "min" in a
    # A has values [100, 250, 500] → mean ≈ $283.33, max = $500, min = $100.
    assert "$283" in a["mean"] or "$284" in a["mean"]
    assert "$500" in a["max"]
    assert "$100" in a["min"]


def test_summarize_by_group_bad_op():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="median",
    )
    assert "Unsupported op" in raw


def test_summarize_by_group_no_rows_after_filter():
    _setup_spend_fixture()
    raw = data_tools._summarize_by_group_impl(
        table_name="spends_data", value_column="Amount",
        group_column="Merchant Name", op="sum",
        filter_column="Merchant Name", filter_value="ZZZ-nope",
    )
    assert "no rows match" in raw
