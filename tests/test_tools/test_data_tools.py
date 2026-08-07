"""Tests for tools/data_tools.py (case-scoped)."""

import json

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


# ── Schema cache ────────────────────────────────────────────────────────────


def test_get_schema_cache_returns_identical_result_on_repeat_calls():
    """Two back-to-back calls for the same table on the same case must
    return byte-identical strings — the cache hit path can't change the
    result. This is the correctness side of the perf optimization."""
    a = data_tools._get_table_schema_impl("bureau_full")
    b = data_tools._get_table_schema_impl("bureau_full")
    assert a == b
    # Same key in the cache, single entry per table.
    keys = {k for k in data_tools._schema_cache.keys() if k[1] == "bureau_full"}
    assert len(keys) == 1


def test_get_schema_cache_is_per_case():
    """Switching cases must NOT serve a stale schema from a different case
    — the cache key includes ``case_id`` so cross-case contamination is
    impossible by construction."""
    # Initial call on CASE-00001 → cached.
    out1 = data_tools._get_table_schema_impl("bureau_full")
    case1 = data_tools._gateway.get_case_id()

    # Switch to CASE-00002 (the fixture also has bureau_full there).
    data_tools._gateway.set_case("CASE-00002")
    out2 = data_tools._get_table_schema_impl("bureau_full")
    case2 = data_tools._gateway.get_case_id()

    assert case1 != case2
    # Both runs cached under their own key.
    assert (case1, "bureau_full") in data_tools._schema_cache
    assert (case2, "bureau_full") in data_tools._schema_cache
    # CASE-00002 has only one row (per the fixture), so its schema may
    # differ structurally from CASE-00001's two-row table — at the very
    # least the inputs are different cases. Either way the cache keys
    # don't collide.
    _ = (out1, out2)  # silence unused-var; we only assert key separation here


def test_init_tools_clears_schema_cache():
    """Re-initializing the module wires fresh state — the previous cache
    must be flushed so a different gateway/catalog can't return stale
    results memoized under the old wiring."""
    data_tools._get_table_schema_impl("bureau_full")
    assert len(data_tools._schema_cache) > 0
    # Re-init with the same gateway / catalog (idempotent, but should clear).
    data_tools.init_tools(data_tools._gateway, data_tools._catalog)
    assert data_tools._schema_cache == {}


def test_clear_schema_cache_drops_all_entries():
    data_tools._get_table_schema_impl("bureau_full")
    assert len(data_tools._schema_cache) > 0
    data_tools.clear_schema_cache()
    assert data_tools._schema_cache == {}


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


def test_query_table_dedup_guard_short_circuits_identical_repeat():
    """Within a turn, re-issuing the EXACT same query_table returns a directive
    to change the query — not another full re-dump. (Fixes the observed trickle
    where a specialist ran the same unfiltered query 4 rounds in a row.)"""
    from tools.node_trace.core import TURN_SCOPE, TurnScope
    # Reset the module-level guard state so the test is order-independent.
    data_tools._recent_call_turn = None
    data_tools._recent_call_sigs = {}
    token = TURN_SCOPE.set(TurnScope(chat_id="c", case_id="CASE-00001", turn_id="T-DEDUP"))
    try:
        first = json.loads(data_tools._query_table_impl("bureau_full"))
        assert "repeated_call" not in first          # first call: real data
        assert first["rows_matching_filter"] == 2
        second = json.loads(data_tools._query_table_impl("bureau_full"))
        assert second.get("repeated_call") is True    # identical repeat: directive
        assert "IDENTICAL" in second["message"]
        # A DIFFERENT query in the same turn is NOT blocked.
        other = json.loads(
            data_tools._query_table_impl("bureau_full", filter_column="score", filter_value=720))
        assert "repeated_call" not in other
    finally:
        TURN_SCOPE.reset(token)
        data_tools._recent_call_turn = None
        data_tools._recent_call_sigs = {}


def test_query_table_dedup_guard_inert_without_turn_scope():
    """With no active turn scope (unit tests / ad-hoc), the guard is inert — the
    same query twice returns real data both times, never the directive."""
    a = json.loads(data_tools._query_table_impl("bureau_full"))
    b = json.loads(data_tools._query_table_impl("bureau_full"))
    assert "repeated_call" not in a and "repeated_call" not in b


def test_query_table_unsorted_truncation_samples_evenly_not_head():
    """Systemic anti-truncation-misread: a large UNSORTED result is sampled
    EVENLY across the match set, so the shown rows SPAN the full range instead of
    clustering on the first rows' value. Regression for the recurring 'reported N
    transactions on one date at one score' hallucination."""
    pad = "p" * 60
    early = [{"trans_dt": "2024-02-25", "score": 20.4, "pad": pad} for _ in range(30)]
    later = [{"trans_dt": f"2025-{(i % 12) + 1:02d}-15", "score": 30 + i, "pad": pad}
             for i in range(100)]
    case_data = {"CASE-TR": {"txns": early + later}}
    gw = LocalDataGateway(case_data=case_data)
    gw.set_case("CASE-TR")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))

    out = json.loads(data_tools._query_table_impl("txns"))       # no filter, no sort
    assert out["rows_matching_filter"] == 130
    assert out["truncated"] is True
    dates = {r["trans_dt"] for r in out["rows"]}
    assert len(dates) > 1                                          # spread, not a cluster
    assert any(d.startswith("2025") for d in dates)               # reaches the far end
    assert "EVENLY-SPACED" in out["truncation_note"]


def test_query_table_sorted_truncation_keeps_top_n_head():
    """When the caller sorted, truncation keeps the HEAD (the intended top-N),
    NOT an even sample — sort intent wins."""
    pad = "p" * 60
    rows = [{"v": i, "pad": pad} for i in range(200)]
    gw = LocalDataGateway(case_data={"CASE-S": {"t": rows}})
    gw.set_case("CASE-S")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    out = json.loads(data_tools._query_table_impl("t", sort_by="v", sort_desc=True))
    assert out["truncated"] is True
    # highest values first, contiguous (head of the sorted list), not spread out
    top = [r["v"] for r in out["rows"]]
    assert top[0] == 199 and top == list(range(199, 199 - len(top), -1))


def _setup_txn_fixture():
    """A transaction-like table (date + score) for compound-filter / sort /
    limit tests — mirrors the extraction question that previously thrashed."""
    case_data = {
        "CASE-TXN": {
            "txns": [
                {"trans_dt": "2025-05-03", "tsr": 25.0, "amt": 100},
                {"trans_dt": "2025-05-10", "tsr": 30.0, "amt": 200},
                {"trans_dt": "2025-05-20", "tsr": 15.0, "amt": 300},
                {"trans_dt": "2025-04-15", "tsr": 40.0, "amt": 400},  # out of window
                {"trans_dt": "2025-05-25", "tsr": 22.0, "amt": 500},
            ],
        },
    }
    gateway = LocalDataGateway(case_data=case_data)
    gateway.set_case("CASE-TXN")
    data_tools.init_tools(gateway, DataCatalog(profile_dir="config/data_profiles"))


def test_query_table_compound_filters_are_anded():
    """`filters` ANDs multiple conditions in ONE call — the fix for the
    extraction thrash (date window AND threshold, previously impossible)."""
    _setup_txn_fixture()
    out = json.loads(data_tools._query_table_impl(
        "txns",
        filters=json.dumps([
            {"column": "trans_dt", "op": "between", "value": "2025-05-01,2025-05-31"},
            {"column": "tsr", "op": "gt", "value": "20"},
        ]),
    ))
    # May-2025 AND tsr>20 → rows with tsr 25/30/22 (not the 15, not the April 40).
    assert out["rows_matching_filter"] == 3
    assert " AND " in out["filter"]


def test_query_table_sort_and_limit_returns_top_n():
    _setup_txn_fixture()
    out = json.loads(data_tools._query_table_impl(
        "txns",
        filter_column="trans_dt", filter_value="2025-05-01,2025-05-31",
        filter_op="between", sort_by="tsr", sort_desc=True, limit=2,
    ))
    assert out["rows_matching_filter"] == 4     # true match count, BEFORE the limit
    assert out["limit"] == 2
    assert out["sort"] == "tsr desc"
    assert [r["tsr"] for r in out["rows"]] == [30.0, 25.0]   # top-2 by tsr desc


def test_batch_query_table_runs_multiple_queries():
    _setup_txn_fixture()
    specs = [
        {"table_name": "txns", "filter_column": "tsr", "filter_op": "gt", "filter_value": "30"},
        {"table_name": "txns", "filters": [{"column": "trans_dt", "op": "contains", "value": "2025-05"}]},
    ]
    out = json.loads(data_tools._batch_query_table_impl(json.dumps(specs)))
    assert len(out["results"]) == 2
    assert json.loads(out["results"][0]["result"])["rows_matching_filter"] == 1  # tsr>30 → only the 40
    assert json.loads(out["results"][1]["result"])["rows_matching_filter"] == 4  # 4 May rows


def test_batch_query_table_invalid_json():
    out = json.loads(data_tools._batch_query_table_impl("not json"))
    assert out["error"] == "specs_unparseable"
    assert "fabricat" in out["REQUIRED"].lower()  # forbids inventing values


def test_filter_eq_timestamp_matches_at_second_precision():
    """A spend's millisecond `Timestamp` must join to the model table's
    second-precision `txn_date_time`, matching the ONE transaction at that
    second — not the whole day. Regression: `_date_key` collapsed both to day
    grain, so an exact-timestamp score lookup returned every same-day row and
    the specialist reported 'no transaction-level scores found'.
    """
    rows = [
        {"txn_date_time": "2025-05-14 11:35:35", "tsr": 11.0},   # S BERTRAM
        {"txn_date_time": "2025-05-14 09:57:07", "tsr": 5.0},
        {"txn_date_time": "2025-05-13 11:35:35", "tsr": 9.0},
    ]
    res = data_tools._apply_filter(
        rows, "txn_date_time", "2025-05-14 11:35:35.101", "eq")
    assert [r["tsr"] for r in res] == [11.0]


def test_filter_eq_date_only_still_matches_whole_day():
    """Date-only filters keep day-grain matching (a `2025-05-14` filter matches
    a `2025-05-14 HH:MM:SS` cell) — the second-precision path must not regress
    this behavior the catalog relies on."""
    rows = [
        {"txn_date_time": "2025-05-14 11:35:35", "tsr": 11.0},
        {"txn_date_time": "2025-05-14 09:57:07", "tsr": 5.0},
        {"txn_date_time": "2025-05-13 11:35:35", "tsr": 9.0},
    ]
    res = data_tools._apply_filter(rows, "txn_date_time", "2025-05-14", "eq")
    assert sorted(r["tsr"] for r in res) == [5.0, 11.0]


def test_filter_in_op_matches_any_of_list():
    """`in` is the multi-key `eq`: look up several transactions in one call.
    A millisecond value in the list matches a second-precision cell."""
    _setup_txn_fixture()
    out = json.loads(data_tools._query_table_impl(
        "txns", filter_column="trans_dt",
        filter_value="2025-05-03,2025-05-20", filter_op="in"))
    assert out["rows_matching_filter"] == 2  # the 05-03 and 05-20 rows


def _setup_join_fixture(case="CASE-J", extra_spend=None):
    spend = [
        {"Timestamp": "2025-05-14 11:35:35.101", "merchant": "S BERTRAM", "amount": 50220},
        {"Timestamp": "2025-05-27 08:52:15.925", "merchant": "FRAYLICH", "amount": 37430},
    ]
    if extra_spend:
        spend.append(extra_spend)
    case_data = {case: {
        "spend": spend,
        "scores": [
            {"txn_date_time": "2025-05-14 11:35:35", "tsr": 11.0},   # no ms
            {"txn_date_time": "2025-05-27 08:52:15", "tsr": 4.3},
        ],
    }}
    gw = LocalDataGateway(case_data=case_data)
    gw.set_case(case)
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))


def test_join_table_attaches_right_columns_by_timestamp():
    """Inner join across the ms↔second timestamp — attaches model scores to
    each spend in ONE call (the manual-join pattern, coded)."""
    _setup_join_fixture()
    out = json.loads(data_tools._join_table_impl(
        "spend", "scores", left_on="Timestamp", right_on="txn_date_time",
        columns="merchant,amount,tsr"))
    assert out["matched_rows"] == 2
    assert {r["merchant"]: r["tsr"] for r in out["rows"]} == {
        "S BERTRAM": 11.0, "FRAYLICH": 4.3}


def test_join_table_inner_drops_unmatched_left():
    """A left row with no matching score is dropped by an inner join, kept by
    a left join."""
    _setup_join_fixture(
        extra_spend={"Timestamp": "2025-05-01 00:00:00.000", "merchant": "NOSCORE", "amount": 10})
    inner = json.loads(data_tools._join_table_impl(
        "spend", "scores", left_on="Timestamp", right_on="txn_date_time"))
    assert inner["matched_rows"] == 2
    left = json.loads(data_tools._join_table_impl(
        "spend", "scores", left_on="Timestamp", right_on="txn_date_time", how="left"))
    assert left["matched_rows"] == 3  # unmatched NOSCORE row kept


def test_join_table_missing_table():
    _setup_join_fixture()
    out = data_tools._join_table_impl("spend", "no_such", left_on="Timestamp")
    assert "unavailable" in out.lower()


def _setup_txn_detail_fixture():
    """spends + per-transaction scores + drivers, joinable on the timestamp —
    named with the canonical table names the tool resolves."""
    case_data = {"CASE-TD": {
        "spends": [
            {"Timestamp": "2025-05-14 11:35:35.101", "Date": "2025-05-14",
             "Merchant Name": "S BERTRAM", "Amount": 50220},
            {"Timestamp": "2025-05-27 08:52:15.925", "Date": "2025-05-27",
             "Merchant Name": "FRAYLICH", "Amount": 37430},
        ],
        "model_scores_transaction": [
            # CDSS = credit_loss_prob (the customer score); cust_eff_se_cdss is a
            # separate MERCHANT score and is NOT in transaction_detail's default set.
            {"txn_date_time": "2025-05-14 11:35:35", "tot_struct_risk_score": 11.0,
             "credit_loss_prob": 0.9},
            {"txn_date_time": "2025-05-27 08:52:15", "tot_struct_risk_score": 4.3,
             "credit_loss_prob": 0.4},
        ],
        "score_drivers_transaction": [
            {"txn_date_time": "2025-05-14 11:35:35.101", "top_cdss1": "cashA", "top_tsr1": "pdA"},
            {"txn_date_time": "2025-05-27 08:52:15.925", "top_cdss1": "cashB", "top_tsr1": "pdB"},
        ],
    }}
    gw = LocalDataGateway(case_data=case_data)
    gw.set_case("CASE-TD")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))


def test_transaction_detail_by_timestamps_merges_scores_and_drivers():
    _setup_txn_detail_fixture()
    out = json.loads(data_tools._transaction_detail_impl(
        timestamps="2025-05-14 11:35:35.101,2025-05-27 08:52:15.925"))
    assert out["transactions_selected"] == 2
    assert out["with_model_scores"] == 2
    by_m = {r["Merchant Name"]: r for r in out["rows"]}
    assert by_m["S BERTRAM"]["tot_struct_risk_score"] == 11.0        # ms↔second join
    assert by_m["S BERTRAM"]["credit_loss_prob"] == 0.9              # CDSS = customer score
    assert "cust_eff_se_cdss_5_180_day_score" not in by_m["S BERTRAM"]  # merchant score excluded
    assert by_m["S BERTRAM"]["top_cdss1"] == "cashA"                  # drivers merged
    assert by_m["FRAYLICH"]["top_tsr1"] == "pdB"


def test_transaction_detail_by_spend_filter():
    _setup_txn_detail_fixture()
    out = json.loads(data_tools._transaction_detail_impl(
        filter_column="Merchant Name", filter_op="contains", filter_value="FRAYLICH"))
    assert out["transactions_selected"] == 1
    r = out["rows"][0]
    assert r["Amount"] == 37430 and r["tot_struct_risk_score"] == 4.3
    assert r["top_tsr1"] == "pdB"


def test_transaction_detail_base_model_selects_by_score_and_left_joins():
    """base_table='model_scores_transaction' selects by a MODEL metric (TSR) and
    LEFT-joins spends + drivers. A model-scored txn with no settled spend keeps
    its scores/drivers; merchant/amount is simply absent (NOT a failure), and
    joined_match_counts reports coverage. (Fixes "unable to retrieve drivers or
    merchant/amount due to missing data in the join" for TSR-selected txns.)"""
    case_data = {"CASE-M": {
        "spends": [
            {"Timestamp": "2025-05-14 11:35:35.101", "Date": "2025-05-14",
             "Merchant Name": "S BERTRAM", "Amount": 50220},
        ],
        "model_scores_transaction": [
            {"txn_date_time": "2025-05-14 11:35:35", "tot_struct_risk_score": 39.6,
             "credit_loss_prob": 2.4},   # has a settled spend
            {"txn_date_time": "2025-05-20 08:00:00", "tot_struct_risk_score": 25.0,
             "credit_loss_prob": 1.1},   # model-only (auth/decline) — NO spend
        ],
        "score_drivers_transaction": [
            {"txn_date_time": "2025-05-14 11:35:35", "top_cdss1": "cbr_score", "top_tsr1": "cbr_score"},
            {"txn_date_time": "2025-05-20 08:00:00", "top_cdss1": "times_30_dpd", "top_tsr1": "times_30_dpd"},
        ],
    }}
    gw = LocalDataGateway(case_data=case_data)
    gw.set_case("CASE-M")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    out = json.loads(data_tools._transaction_detail_impl(
        base_table="model_scores_transaction",
        filter_column="tot_struct_risk_score", filter_op="gt", filter_value="20",
        sort_by="tot_struct_risk_score", sort_desc=True))
    assert out["transactions_selected"] == 2                                  # both TSR>20
    assert out["joined_match_counts"]["score_drivers_transaction"] == 2       # drivers for BOTH
    assert out["joined_match_counts"]["spends"] == 1                          # only one settled
    rows = out["rows"]
    assert rows[0]["tot_struct_risk_score"] == 39.6                           # highest first
    assert rows[0]["Merchant Name"] == "S BERTRAM" and rows[0]["top_cdss1"] == "cbr_score"
    # model-only txn: scores + drivers + time present, merchant/amount absent (not a failure)
    assert "Merchant Name" not in rows[1] and rows[1]["top_cdss1"] == "times_30_dpd"
    assert rows[1]["txn_date_time"] == "2025-05-20 08:00:00"


def test_transaction_detail_extraction_returns_many_rows_and_coverage():
    """Regression: extracting the abnormal TSR-reacted transactions must return a
    USABLE number of joined rows, not collapse to ~3 under the size cap — and it
    must not drop every settled spend when sorted by TSR desc (the extreme rows
    are model-only declines). Reproduces "merchant/amount missing due to join
    limitations": here the TOP-TSR rows are declines (no spend) and the settled
    rows sit lower; head-truncation kept only the declines. Even-sampling + the
    bigger cap keep settled rows, and `merchant_amount_coverage` states the split.
    """
    # Settled and decline transactions span the SAME TSR range (20-39), as in the
    # real book — so a TSR-desc top-N selection includes BOTH, and the test checks
    # that even-sampling doesn't silently drop the settled (merchant-bearing) ones.
    spends = [
        {"Timestamp": f"2025-04-{d:02d} 10:00:00", "Date": f"2025-04-{d:02d}",
         "Merchant Name": f"MERCH {d}", "Amount": 100 + d}
        for d in range(1, 41)
    ]
    model = (
        [{"txn_date_time": f"2025-04-{d:02d} 10:00:00",
          "tot_struct_risk_score": 20.0 + d * 0.45, "credit_loss_prob": 1.0}
         for d in range(1, 41)]                                   # settled, TSR ~20-38
        + [{"txn_date_time": f"2025-04-{d:02d} 22:00:00",
            "tot_struct_risk_score": 20.2 + d * 0.45, "credit_loss_prob": 2.0}
           for d in range(1, 41)]                                 # declines, TSR ~20-38
    )
    gw = LocalDataGateway(case_data={"CASE-X": {"spends": spends,
                                                "model_scores_transaction": model}})
    gw.set_case("CASE-X")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    out = json.loads(data_tools._transaction_detail_impl(
        base_table="model_scores_transaction",
        filter_column="tot_struct_risk_score", filter_op="gte", filter_value="20",
        sort_by="tot_struct_risk_score", sort_desc=True, limit=40))
    assert out["transactions_selected"] == 80
    # Far more than the old ~3-row collapse.
    assert out["rows_returned"] >= 15
    # Coverage is reported from the full join, and the impl must NOT have dropped
    # every settled spend: at least some returned rows carry a merchant.
    assert "merchant_amount_coverage" in out
    assert sum(1 for r in out["rows"] if r.get("Merchant Name")) >= 1


def test_batch_aggregate_runs_multiple_scalars_in_one_call():
    specs = [
        {"table_name": "bureau_full", "column": "score", "op": "count"},
        {"table_name": "bureau_full", "column": "score", "op": "min"},
        {"table_name": "bureau_full", "column": "score", "op": "max"},
        {
            "table_name": "bureau_full",
            "column": "derog_count",
            "op": "sum",
            "filter_column": "score",
            "filter_value": "720",
        },
    ]

    result = data_tools._batch_aggregate_impl(json.dumps(specs))
    payload = json.loads(result)

    assert len(payload["results"]) == 4
    rendered = "\n".join(r["result"] for r in payload["results"])
    assert "count" in rendered
    assert "2" in rendered
    assert "min(score)" in rendered
    assert "max(score)" in rendered
    assert "filtered by score eq '720'" in rendered


def test_batch_aggregate_rejects_invalid_json():
    result = data_tools._batch_aggregate_impl("not json")
    payload = json.loads(result)
    assert payload["error"] == "specs_unparseable"
    assert "fabricat" in payload["REQUIRED"].lower()


# ── summarize_trend ──────────────────────────────────────────────────────


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
    assert s["peak_all_time"]["period"] == "2025-03"     # 1100 is the max
    assert s["trough_all_time"]["period"] == "2024-12"   # 250 is the min


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


def _setup_long_series_fixture(period_kind: str):
    """Build a synthetic series long enough to blow past _TREND_MAX_CHARS.

    `period_kind` = 'month' → 120 monthly rows (10 years); 'day' → 4000 daily
    rows. Each row a distinct date + a varying value so buckets are unique.
    """
    rows = []
    if period_kind == "month":
        for i in range(120):
            y = 2016 + i // 12
            m = i % 12 + 1
            rows.append({"Date": f"{y:04d}-{m:02d}-15", "Amount": float(100 + i)})
    else:  # day
        import datetime
        base = datetime.date(2016, 1, 1)
        for i in range(4000):
            d = base + datetime.timedelta(days=i)
            rows.append({"Date": d.isoformat(), "Amount": float(100 + i)})
    gateway = LocalDataGateway(case_data={"CASE-LONG": {"spends_data": rows}})
    gateway.set_case("CASE-LONG")
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)


def test_summarize_trend_monthly_never_downsampled():
    """Coarse-grain (month) series must survive WHOLE, even past the size cap —
    down-sampling drops interior months the specialist then can't narrate."""
    _setup_long_series_fixture("month")
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    assert len(raw) > data_tools._TREND_MAX_CHARS  # genuinely over the budget
    payload = json.loads(raw)
    # Every one of the 120 months is present; nothing sampled away.
    assert payload["summary"]["n_buckets"] == 120
    assert len(payload["series"]) == 120
    assert "series_note" not in payload
    assert payload["series"][0]["period"] == "2016-01"
    assert payload["series"][-1]["period"] == "2025-12"


def test_summarize_trend_daily_still_downsampled():
    """Day-grain can explode unboundedly, so it still falls back to uniform
    down-sampling — but keeps the first & last bucket (full range preserved)."""
    _setup_long_series_fixture("day")
    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="day", op="sum",
    )
    payload = json.loads(raw)
    assert len(raw) <= data_tools._TREND_MAX_CHARS
    assert "series_note" in payload
    assert len(payload["series"]) < payload["summary"]["n_buckets"]
    # First & last survive the sampling → date range intact.
    assert payload["series"][0]["period"] == "2016-01-01"
    assert payload["series"][-1]["period"] == "2026-12-13"


def test_batch_summarize_trend_runs_multiple_trends_in_one_call():
    _setup_spend_fixture()
    specs = [
        {"table_name": "spends_data", "value_column": "Amount",
         "time_column": "Date", "period": "month", "op": "sum"},
        {"table_name": "spends_data", "value_column": "Amount",
         "time_column": "Date", "period": "month", "op": "count"},
    ]
    raw = data_tools._batch_summarize_trend_impl(json.dumps(specs))
    payload = json.loads(raw)
    assert len(payload["results"]) == 2

    # Each entry echoes the source value_column so the LLM can correlate
    # result→indicator without re-parsing the spec.
    assert payload["results"][0]["value_column"] == "Amount"
    assert payload["results"][1]["value_column"] == "Amount"

    # The per-trend `result` field carries the same JSON document a
    # single summarize_trend call would have returned.
    sum_trend = json.loads(payload["results"][0]["result"])
    count_trend = json.loads(payload["results"][1]["result"])
    assert sum_trend["op"] == "sum"
    assert count_trend["op"] == "count"
    assert sum_trend["summary"]["n_buckets"] == 4
    # Mar-2025 sum = 1100; count = 2.
    sum_mar = next(s for s in sum_trend["series"] if s["period"] == "2025-03")
    count_mar = next(s for s in count_trend["series"] if s["period"] == "2025-03")
    assert sum_mar["raw_value"] == 1100.0
    assert count_mar["raw_value"] == 2


def test_batch_summarize_trend_caps_at_six_specs():
    _setup_spend_fixture()
    spec = {"table_name": "spends_data", "value_column": "Amount",
            "time_column": "Date", "period": "month", "op": "sum"}
    raw = data_tools._batch_summarize_trend_impl(json.dumps([spec] * 8))
    payload = json.loads(raw)
    assert len(payload["results"]) == 6
    assert "truncated" in payload
    assert "first 6 of 8" in payload["truncated"]


def test_batch_summarize_trend_rejects_invalid_json():
    raw = data_tools._batch_summarize_trend_impl("not json")
    payload = json.loads(raw)
    assert payload["error"] == "specs_unparseable"
    assert "fabricat" in payload["REQUIRED"].lower()


def test_batch_summarize_trend_rejects_non_list():
    raw = data_tools._batch_summarize_trend_impl(json.dumps({"table_name": "x"}))
    payload = json.loads(raw)
    assert payload["error"] == "specs_unparseable"


def test_batch_summarize_trend_truncated_specs_returns_directive_not_fabrication():
    """The confirmed safechain failure: specs_json arrives TRUNCATED to '['
    (json.loads → 'Expecting value: line 1 column 2'). The tool must return a
    hard anti-fabrication directive (so the specialist retries), NOT a neutral
    error it answers around by inventing peak values."""
    raw = data_tools._batch_summarize_trend_impl("[")
    payload = json.loads(raw)
    assert payload["error"] == "specs_unparseable"
    assert "did NOT run" in payload["message"]
    assert "data_gap" in payload["REQUIRED"]


def test_salvage_specs_recovers_complete_objects_and_accepts_list():
    # Already a parsed list (safechain sometimes passes the array, not a string).
    assert data_tools._salvage_specs_list([{"a": 1}]) == [{"a": 1}]
    # Truncated after a complete object → recover the complete one, drop the partial.
    salv = data_tools._salvage_specs_list('[{"table_name":"t","op":"max"},{"table_name":')
    assert salv == [{"table_name": "t", "op": "max"}]
    # Bare '[' → nothing complete → empty (caller emits the directive).
    assert data_tools._salvage_specs_list("[") == []


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


# ── Date-format coverage (extends _date_key for private-env formats) ────────


@pytest.mark.parametrize("date_fmt,expected_periods", [
    # ISO datetime — private-env warehouses commonly export with timestamp.
    (
        ["2024-11-05 10:30:00", "2024-11-20 14:00:00", "2024-12-10T08:15:30Z"],
        ["2024-11", "2024-12"],
    ),
    # ISO with slash separator.
    (["2024/11/05", "2024/11/20", "2024/12/10"], ["2024-11", "2024-12"]),
    # US-style MM/DD/YYYY.
    (["11/05/2024", "11/20/2024", "12/10/2024"], ["2024-11", "2024-12"]),
    # Numeric dash MM-DD-YYYY.
    (["11-05-2024", "11-20-2024", "12-10-2024"], ["2024-11", "2024-12"]),
    # Compact ISO basic YYYYMMDD.
    (["20241105", "20241120", "20241210"], ["2024-11", "2024-12"]),
    # US-slash date WITH trailing time, 2-digit year — the format the
    # `strategy` table ships in. "5/28/24 3:03" / "2/15/25 14:22".
    # The time portion is sub-resolution (we bucket by month) — strip
    # it and reuse the MM/DD/YY parsing.
    (["11/05/24 3:03", "11/20/24 14:22", "12/10/24 9:00"],
     ["2024-11", "2024-12"]),
    # MMM-YY (2-digit year): "Nov-24", "Dec-24".
    (["Nov-24", "Nov-24", "Dec-24"], ["2024-11", "2024-12"]),
    # YYYY-MMM: "2024-Nov", "2024-Dec".
    (["2024-Nov", "2024-Nov", "2024-Dec"], ["2024-11", "2024-12"]),
    # Excel serial dates: 45292 = 2024-01-01, 45323 = 2024-02-01.
    (["45292", "45292", "45323"], ["2024-01", "2024-02"]),
    # DD-MMM-YY / D-MMM-YY (2-digit year) — the private-env
    # `payments.payment_date` format: "7-Jul-24", "16-Jul-24". The
    # 4-digit-year DD-MMM-YYYY form was already covered; the 2-digit-year
    # variant was NOT, so the monthly payment trend came back empty and the
    # spend-vs-payment chart only showed the spend line.
    (["7-Jul-24", "16-Jul-24", "3-Aug-24"], ["2024-07", "2024-08"]),
])
def test_summarize_trend_handles_extended_date_formats(date_fmt, expected_periods):
    """Regression: private environment hits formats beyond the original
    ISO/DD-MMM-YYYY set. Each form must bucket into the same months."""
    case_data = {"CASE-X": {"spends_data": [
        {"Date": d, "Amount": 100.0} for d in date_fmt
    ]}}
    gateway = LocalDataGateway(case_data=case_data)
    gateway.set_case("CASE-X")
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)

    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    payload = json.loads(raw)
    assert [s["period"] for s in payload["series"]] == expected_periods


def test_summarize_trend_surfaces_unparseable_samples():
    """When _date_key fails for every row, the user-facing string must surface
    actual sample values (not just "no parseable values") so the operator can
    diagnose which format the parser doesn't recognize. Without this, the LLM
    sees only the opaque message and the fix path is invisible."""
    case_data = {"CASE-BAD": {"spends_data": [
        # Pick formats _date_key intentionally rejects so we test the failure
        # path (RFC 2822-ish, sentence form).
        {"Date": "Wed, 16 Nov 2024 10:30:00", "Amount": 100.0},
        {"Date": "November 16th, 2024", "Amount": 200.0},
        {"Date": "November 16th, 2024", "Amount": 250.0},  # duplicate dropped
    ]}}
    gateway = LocalDataGateway(case_data=case_data)
    gateway.set_case("CASE-BAD")
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)

    raw = data_tools._summarize_trend_impl(
        table_name="spends_data", value_column="Amount", time_column="Date",
        period="month", op="sum",
    )
    assert "no parseable Date values" in raw
    # The unrecognized samples must appear so the operator can extend the
    # parser (or normalize at ingestion).
    assert "Wed, 16 Nov 2024 10:30:00" in raw
    assert "November 16th, 2024" in raw
    # Counts must reflect every failing row, even the duplicate.
    assert "3 row(s) had unrecognized Date format" in raw


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


# ── Task 1: _apply_filter — case-insensitive eq/ne, contains op, null ne ────


from tools.data_tools import _apply_filter


def _rows():
    return [
        {"merchant": "WALMART", "amt": 10, "flag": None},
        {"merchant": " Walmart ", "amt": 20, "flag": "x"},
        {"merchant": "Target", "amt": 30, "flag": None},
    ]


def test_eq_is_case_and_whitespace_insensitive_for_text():
    out = _apply_filter(_rows(), "merchant", "walmart", "eq")
    assert len(out) == 2  # "WALMART" and " Walmart "


def test_ne_excludes_case_insensitive_matches_and_counts_nulls():
    out = _apply_filter(_rows(), "merchant", "walmart", "ne")
    # Only "Target" differs by value; null cells also satisfy ne.
    merchants = sorted(str(r["merchant"]) for r in out)
    assert merchants == ["Target"]


def test_ne_counts_null_cells():
    out = _apply_filter(_rows(), "flag", "x", "ne")
    # the row with flag=="x" is excluded; the 2 null-flag rows satisfy ne
    assert len(out) == 2


def test_contains_matches_substring_case_insensitively():
    rows = [{"m": "STARBUCKS #4412 SEATTLE WA"}, {"m": "WALMART"}]
    out = _apply_filter(rows, "m", "starbucks", "contains")
    assert len(out) == 1 and out[0]["m"].startswith("STARBUCKS")


def test_numeric_eq_still_exact_after_string_changes():
    rows = [{"code": 0}, {"code": 1}, {"code": 1}]
    assert len(_apply_filter(rows, "code", "1", "eq")) == 2
    assert len(_apply_filter(rows, "code", "0", "eq")) == 1


# ── Task 2: _coerce_pair — gate numeric coercion for ID/code columns ─────────


from tools.data_tools import _coerce_pair


def test_leading_zero_ids_stay_strings():
    a, b = _coerce_pair("007", "7")
    assert a != b  # not coerced to 7.0 == 7.0


def test_plain_numbers_still_coerce():
    assert _coerce_pair(0, "0") == (0.0, 0.0)
    assert _coerce_pair("0.7", 0.7) == (0.7, 0.7)
    assert _coerce_pair("188800", "188800") == (188800.0, 188800.0)


def test_scientific_and_inf_nan_not_numeric():
    a, b = _coerce_pair("1e3", "1000")
    assert a != b  # "1e3" stays a string, not 1000.0


# ── Task 3: _resolve_real_column — refuse ambiguous fuzzy match ──────────────


from tools.data_tools import _resolve_real_column


def test_fuzzy_column_refuses_ambiguous_match():
    rows = [{"score_1": 1, "score_2": 2}]
    # "score_1" exists exactly → returns it.
    assert _resolve_real_column(rows, "score_1", None) == "score_1"
    # "score_9" doesn't exist; "score_1" and "score_2" both normalize to
    # "score" → ambiguous → refuse, return the literal (honest miss).
    assert _resolve_real_column(rows, "score_9", None) == "score_9"


def test_fuzzy_column_unique_match_still_resolves():
    rows = [{"Merchant Risk Score": 0.5}]
    assert _resolve_real_column(rows, "merchant_risk_score", None) == "Merchant Risk Score"


# ── Task 4: _date_key — MMM-YY, YYYY-MMM, Excel serial, confirm covered ──────


from tools.data_tools import _date_key


def test_date_key_mmm_yy():
    assert _date_key("Jul-25") == (2025, 7, 1)
    assert _date_key("Jul'25") == (2025, 7, 1)


def test_date_key_year_mmm():
    assert _date_key("2025-Jul") == (2025, 7, 1)


def test_date_key_excel_serial():
    # 45292 = 2024-01-01 (Excel epoch 1899-12-30)
    assert _date_key("45292") == (2024, 1, 1)


def test_date_key_mmm_yyyy_already_covered():
    assert _date_key("Jul-2025") == (2025, 7, 1)


def test_date_key_day_mmm_2digit_year():
    # Private-env `payments.payment_date`: "7-Jul-24", "16-Jul-24", "22-Jul-24".
    assert _date_key("7-Jul-24") == (2024, 7, 7)
    assert _date_key("16-Jul-24") == (2024, 7, 16)
    assert _date_key("22-Jul-24") == (2024, 7, 22)
    # 2-digit-year sliding window (same pivot as the slash forms): 49→2049, 50→1950.
    assert _date_key("01-Jan-49") == (2049, 1, 1)
    assert _date_key("01-Jan-50") == (1950, 1, 1)


def test_date_key_tz_aware_datetime_already_covered():
    assert _date_key("2024-01-01 15:25:20.602+00:00") == (2024, 1, 1)
    assert _date_key("2024-01-01T15:25:20Z") == (2024, 1, 1)


# ── summarize_by_group tail aggregate ───────────────────────────────────────
def _group_gateway(rows):
    gw = LocalDataGateway(case_data={"CASE-GRP": {"txns": rows}})
    gw.set_case("CASE-GRP")
    return gw


def test_summarize_by_group_appends_a_tail_aggregate():
    """A share/pie chart built from the top-N must still sum to the whole."""
    rows = [{"merchant": f"M{i}", "amt": float(100 - i)} for i in range(40)]
    data_tools.init_tools(_group_gateway(rows),
                          DataCatalog(profile_dir="config/data_profiles"))

    payload = json.loads(data_tools._summarize_by_group_impl(
        table_name="txns", value_column="amt", group_column="merchant",
        op="sum", top_n=5))

    groups = payload["groups"]
    assert groups[-1]["group"] == "(35 others)"
    total = sum(g["raw_value"] for g in groups)
    assert total == pytest.approx(sum(r["amt"] for r in rows))


def test_summarize_by_group_no_tail_when_all_groups_fit():
    rows = [{"merchant": f"M{i}", "amt": 10.0} for i in range(3)]
    data_tools.init_tools(_group_gateway(rows),
                          DataCatalog(profile_dir="config/data_profiles"))

    payload = json.loads(data_tools._summarize_by_group_impl(
        table_name="txns", value_column="amt", group_column="merchant",
        op="sum", top_n=10))
    assert not any("others" in g["group"] for g in payload["groups"])


def test_summarize_by_group_no_tail_for_non_additive_ops():
    """Summing per-group means is meaningless, so no tail row."""
    rows = [{"merchant": f"M{i}", "amt": float(i)} for i in range(40)]
    data_tools.init_tools(_group_gateway(rows),
                          DataCatalog(profile_dir="config/data_profiles"))

    payload = json.loads(data_tools._summarize_by_group_impl(
        table_name="txns", value_column="amt", group_column="merchant",
        op="mean", top_n=5))
    assert not any("others" in g["group"] for g in payload["groups"])


# ── empty value column is a DATA GAP, not a date-format problem ─────────────

def test_summarize_trend_names_an_empty_value_column_as_a_data_gap():
    """The old message blamed the TIME column whatever the cause, so a
    specialist trending an all-blank column kept 'fixing' a date column that was
    never wrong (observed: bureau_data.'SBFE Score', blank in all 26 rows)."""
    rows = [{"month": "2024-01-01", "score": ""},
            {"month": "2024-02-01", "score": ""}]
    gw = LocalDataGateway(case_data={"CASE-E": {"t": rows}})
    gw.set_case("CASE-E")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))

    out = data_tools._summarize_trend_impl(
        table_name="t", value_column="score", time_column="month", op="max")
    assert "DATA GAP" in out
    assert "'score' is EMPTY" in out
    assert "do NOT retry" in out
    # It must NOT blame the time column — the dates were fine.
    assert "no parseable month" not in out


def test_summarize_trend_still_blames_the_time_column_when_dates_are_bad():
    rows = [{"month": "not-a-date", "score": "5"},
            {"month": "also-bad", "score": "6"}]
    gw = LocalDataGateway(case_data={"CASE-D": {"t": rows}})
    gw.set_case("CASE-D")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))

    out = data_tools._summarize_trend_impl(
        table_name="t", value_column="score", time_column="month", op="max")
    assert "no parseable month values" in out
    assert "DATA GAP" not in out


def test_summarize_trend_with_partial_values_still_trends():
    """Only an EMPTY column is a gap — some blanks is just sparse data."""
    rows = [{"month": "2024-01-01", "score": "5"},
            {"month": "2024-02-01", "score": ""}]
    gw = LocalDataGateway(case_data={"CASE-P": {"t": rows}})
    gw.set_case("CASE-P")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))

    out = data_tools._summarize_trend_impl(
        table_name="t", value_column="score", time_column="month", op="max")
    assert "DATA GAP" not in out
    assert "series" in out


# ── threshold crossings in summarize_trend ──────────────────────────────────
#
# "Did TSR spike recently / cross the threshold?" was unanswerable from the
# summary: `peak_all_time` is global (2024-09 on the real case), `slope` reads
# as declining, and the catalog's risk_threshold never appeared in the output.
# The 2025 breach (Apr 27.4, May 20.2 vs a threshold of 20) was visible only to
# whoever eyeballed 18 raw points AND already knew the threshold.

def _trend_summary(rows, value_column="tot_struct_risk_score",
                   table="model_scores", case="CASE-T"):
    gw = LocalDataGateway(case_data={case: {"model_scores": rows}})
    gw.set_case(case)
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    out = data_tools._summarize_trend_impl(
        table_name=table, value_column=value_column,
        time_column="trans_month", op="max")
    return json.loads(out)["summary"]


def test_trend_reports_threshold_crossings():
    rows = [{"trans_month": f"2025-{m:02d}-01", "tot_struct_risk_score": v}
            for m, v in [(1, 7.4), (2, 12.8), (3, 10.4), (4, 27.4), (5, 20.2)]]
    t = _trend_summary(rows)["threshold"]
    assert t["value"] == 20
    assert t["risky_when"] == "> 20"
    assert t["latest_episode"]["start"] == "2025-04"
    assert t["latest_episode"]["end"] == "2025-05"


def test_latest_breach_is_the_recent_one_not_the_global_peak():
    """The whole point — `peak_all_time` points at 2024; a 'recent' question needs the
    most recent CROSSING."""
    rows = [{"trans_month": f"{y}-{m:02d}-01", "tot_struct_risk_score": v}
            for y, m, v in [(2024, 9, 39.6), (2024, 10, 34.8), (2025, 1, 7.4),
                            (2025, 4, 27.4), (2025, 5, 20.2), (2025, 6, 7.7)]]
    s = _trend_summary(rows)
    assert s["peak_all_time"]["period"] == "2024-09"          # unchanged
    assert s["threshold"]["latest_breach"]["period"] == "2025-05"


def test_threshold_resolves_through_the_real_data_alias():
    """The monthly export ships `tot_struct_risk_score_max`; catalog thresholds
    are keyed by the canonical name. Without alias matching every real-data
    column silently has no threshold."""
    rows = [{"trans_month": "2025-04-01", "tot_struct_risk_score_max": 27.4}]
    t = _trend_summary(rows, value_column="tot_struct_risk_score")["threshold"]
    assert t["value"] == 20 and t["n_breaching_periods"] == 1


def test_below_direction_breaches_when_under_the_limit():
    """`last_cycle_cut_revolve_rate` is risky BELOW 0.46 — an above-only rule
    would report zero breaches on exactly the risky months."""
    rows = [{"trans_month": "2025-01-01", "last_cycle_cut_revolve_rate": 0.80},
            {"trans_month": "2025-02-01", "last_cycle_cut_revolve_rate": 0.31}]
    t = _trend_summary(rows, value_column="last_cycle_cut_revolve_rate")["threshold"]
    assert t["risky_when"].startswith("<")
    assert t["latest_episode"]["start"] == "2025-02"


def test_no_breaches_reports_none_not_a_missing_block():
    rows = [{"trans_month": "2025-01-01", "tot_struct_risk_score": 5.0},
            {"trans_month": "2025-02-01", "tot_struct_risk_score": 6.0}]
    t = _trend_summary(rows)["threshold"]
    assert t["n_breaching_periods"] == 0
    assert t["latest_breach"] is None


def test_column_without_a_catalog_threshold_gets_no_block():
    rows = [{"trans_month": "2025-01-01", "amt": 5.0},
            {"trans_month": "2025-02-01", "amt": 6.0}]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    s = json.loads(data_tools._summarize_trend_impl(
        table_name="t", value_column="amt", time_column="trans_month",
        op="max"))["summary"]
    assert "threshold" not in s


def test_breaching_period_list_is_bounded_but_the_count_is_not():
    rows = [{"trans_month": f"2024-{m:02d}-01", "tot_struct_risk_score": 25.0}
            for m in range(1, 13)]
    rows += [{"trans_month": f"2025-{m:02d}-01", "tot_struct_risk_score": 25.0}
             for m in range(1, 5)]
    t = _trend_summary(rows)["threshold"]
    assert t["n_breaching_periods"] == 16
    # One unbroken run -> ONE episode spanning it, not 16 loose periods.
    assert t["n_episodes"] == 1
    assert (t["latest_episode"]["start"], t["latest_episode"]["end"]) == \
        ("2024-01", "2025-04")


def test_group_total_is_available_as_a_raw_number():
    """`total_across_groups` is a FORMATTED string ("$3,927,582.20"). Anything
    doing share arithmetic needs a number — re-parsing the formatted form is
    exactly the transcription step that goes wrong."""
    rows = [{"m": f"M{i}", "amt": 10.0} for i in range(3)]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    c = json.loads(data_tools._summarize_by_group_impl(
        table_name="t", value_column="amt", group_column="m",
        op="sum", top_n=10))["concentration"]
    assert c["total_across_groups_raw"] == 30.0
    assert isinstance(c["total_across_groups"], str)


def test_sample_advice_matches_the_sampling_actually_used():
    """The advice used to say "FIRST rows in TABLE ORDER" unconditionally,
    contradicting truncation_note in the SAME payload once even-sampling
    landed — two stories about the same rows."""
    rows = [{"m": f"M{i}", "amt": float(i), "pad": "x" * 400} for i in range(200)]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    q = json.loads(data_tools._query_table_impl(table_name="t"))
    assert q["truncated"]
    note, advice = q["truncation_note"], q["count_advice"]
    if "EVENLY-SPACED" in note:
        assert "spread evenly" in advice
        assert "FIRST rows in TABLE ORDER" not in advice
    else:
        assert "FIRST rows in TABLE ORDER" in advice


# ── join / transaction_detail shape defects ─────────────────────────────────

def _two_table_gw(left, right):
    gw = LocalDataGateway(case_data={"C": {"spends": left, "model_scores_transaction": right}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    return gw


def test_join_flags_fan_out_when_the_right_key_repeats():
    """9,021 joined rows from 8,888 spends on the real case, because 64
    timestamps repeat in the transaction table. Unflagged, `matched_rows` reads
    as a transaction count and every downstream total is inflated."""
    left = [{"Timestamp": "2025-01-01 10:00:00", "Amount": "100"}]
    right = [{"txn_date_time": "2025-01-01 10:00:00", "tot_struct_risk_score": "21"},
             {"txn_date_time": "2025-01-01 10:00:00", "tot_struct_risk_score": "22"}]
    _two_table_gw(left, right)
    j = json.loads(data_tools._join_table_impl(
        left_table="spends", right_table="model_scores_transaction",
        left_on="Timestamp", right_on="txn_date_time"))
    assert j["matched_rows"] == 2 and j["left_rows_after_filter"] == 1
    assert j["fan_out"]["detected"] is True
    assert "do NOT count them" in j["fan_out"]["note"]


def test_join_without_fan_out_has_no_flag():
    left = [{"Timestamp": "2025-01-01 10:00:00", "Amount": "100"}]
    right = [{"txn_date_time": "2025-01-01 10:00:00", "tot_struct_risk_score": "21"}]
    _two_table_gw(left, right)
    j = json.loads(data_tools._join_table_impl(
        left_table="spends", right_table="model_scores_transaction",
        left_on="Timestamp", right_on="txn_date_time"))
    assert "fan_out" not in j


def test_transaction_detail_states_the_denominator_of_its_join_counts():
    """`limit=3` reported with_model_scores=3 beside transactions_selected=8888,
    which reads as "only 3 of 8,888 are model-scored"."""
    left = [{"Timestamp": f"2025-01-01 10:00:0{i}", "Amount": "100"} for i in range(5)]
    right = [{"txn_date_time": f"2025-01-01 10:00:0{i}",
              "tot_struct_risk_score": "21"} for i in range(5)]
    _two_table_gw(left, right)
    d = json.loads(data_tools._transaction_detail_impl(limit=2))
    assert d["transactions_selected"] == 5
    assert d["transactions_examined"] == 2
    assert "NOT the 5 selected" in d["joined_match_counts_note"]

    full = json.loads(data_tools._transaction_detail_impl())
    assert full["transactions_examined"] == 5
    assert "NOT the" not in full["joined_match_counts_note"]


def test_driver_values_survive_rows_missing_the_joined_columns():
    """Regression: the resolver cached a column name from a row that HAD it and
    then indexed a row that didn't — `transaction_detail` is a LEFT join, so
    key sets differ. Raised KeyError on the first unmatched row."""
    left = [{"Timestamp": "2025-01-01 10:00:00", "Amount": "100"},
            {"Timestamp": "2025-01-02 10:00:00", "Amount": "200"}]
    right = [{"txn_date_time": "2025-01-01 10:00:00", "cbr_score": "700"}]
    gw = LocalDataGateway(case_data={"C": {
        "spends": left, "model_scores_transaction": right,
        "score_drivers_transaction": [
            {"txn_date_time": "2025-01-01 10:00:00", "top_cdss1": "cbr_score"},
            {"txn_date_time": "2025-01-02 10:00:00", "top_cdss1": "cbr_score"},
        ]}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    d = json.loads(data_tools._transaction_detail_impl())   # must not raise
    assert d["rows_returned"] == 2
    with_vals = [r for r in d["rows"] if "driver_values" in r]
    assert len(with_vals) == 1, "only the joined row can carry a driver value"


# ── zero-match diagnostic ───────────────────────────────────────────────────
#
# A bare `rows_matching_filter: 0` can't be told apart from a wrong column, a
# wrong value vocabulary, or an honest absence — so the skill carried rules like
# "check the column's format FIRST" and "appr_deny_cd is 0/1". The second was
# WRONG on real data (declines are coded 2), which is the case for giving
# feedback instead of instructions: no prompt discipline survives a false fact.

def _decision_gw():
    rows = [{"appr_deny_cd": "0", "amt": "10"}] * 3 + [{"appr_deny_cd": "2", "amt": "20"}]
    gw = LocalDataGateway(case_data={"C": {"model_scores_transaction": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))


def test_zero_match_reports_the_values_actually_present():
    _decision_gw()
    q = json.loads(data_tools._query_table_impl(
        table_name="model_scores_transaction",
        filter_column="appr_deny_cd", filter_value="declined", filter_op="eq"))
    d = q["zero_match_diagnostic"]["appr_deny_cd"]
    assert d["column_exists"] is True
    assert set(d["values_present"]) == {"0", "2"}
    assert d["filter_tried"] == "eq 'declined'"


def test_zero_match_says_when_the_column_does_not_exist():
    _decision_gw()
    q = json.loads(data_tools._query_table_impl(
        table_name="model_scores_transaction",
        filter_column="approval_status", filter_value="1", filter_op="eq"))
    d = q["zero_match_diagnostic"]["approval_status"]
    assert d["column_exists"] is False
    assert "search_columns" in d["hint"]


def test_zero_match_distinguishes_an_empty_column_from_a_bad_filter():
    rows = [{"appr_deny_cd": "", "amt": "10"}]
    gw = LocalDataGateway(case_data={"C": {"model_scores_transaction": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    q = json.loads(data_tools._query_table_impl(
        table_name="model_scores_transaction",
        filter_column="appr_deny_cd", filter_value="2", filter_op="eq"))
    d = q["zero_match_diagnostic"]["appr_deny_cd"]
    assert d["column_is_empty"] is True and "DATA GAP" in d["hint"]


def test_no_diagnostic_when_the_filter_actually_matched():
    _decision_gw()
    q = json.loads(data_tools._query_table_impl(
        table_name="model_scores_transaction",
        filter_column="appr_deny_cd", filter_value="2", filter_op="eq"))
    assert q["rows_matching_filter"] == 1
    assert "zero_match_diagnostic" not in q


def test_schema_declares_the_decline_code_vocabulary():
    """The catalog said 0/1; the real export uses 0/2. Now declared, so
    get_table_schema hands the specialist the real vocabulary up front."""
    _decision_gw()
    sc = json.loads(data_tools._get_table_schema_impl("model_scores_transaction"))
    assert set(sc["appr_deny_cd"]["declared_values"]) == {"0", "2"}
    assert "2 = DECLINED" in sc["appr_deny_cd"]["description"]


# ── ADL alias names resolve to the CAS column ───────────────────────────────
#
# Reviewers ask by the ADL code (`INTOOP`) as often as the CAS name
# (`oop_interaction`). The catalog declares them with inconsistent case
# (`INTOOP`, `CUIDINDX` but `cbsfico`, `curttd01`), so a literal comparison
# missed whichever spelling the caller used — and the miss landed in the DATA
# GAP branch, which tells the specialist "do NOT retry this column" for a
# variable that exists.

_REAL_CASE = "366132845011"


def _real_case_tools():
    gw = LocalDataGateway.from_case_folders("data_tables/real")
    gw.set_case(_REAL_CASE)
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))


@pytest.mark.parametrize("adl,expected", [
    ("INTOOP", "oop_interaction_max"),
    ("intoop", "oop_interaction_max"),          # case must not matter
    ("IntOop", "oop_interaction_max"),
    ("CUIDINDX", "tpf_internal_delinq_idx_max"),
    ("cbsfico", "cbr_score_max"),
    ("CBSFICO", "cbr_score_max"),
    ("NNR1USCR", "cust_rnn_score_max"),
    ("OPENPYDN", "cust_open_acct_paydown_min"),
    ("oop_interaction", "oop_interaction_max"),  # CAS name still works
])
def test_adl_name_trends_the_right_monthly_column(adl, expected):
    _real_case_tools()
    out = data_tools._summarize_trend_impl(
        table_name="model_scores", value_column=adl,
        time_column="trans_month", op="max")
    assert json.loads(out)["value_column"] == expected


@pytest.mark.parametrize("adl,expected", [
    ("INTOOP", "oop_interaction"),
    ("intoop", "oop_interaction"),
    ("CULNBLND", "cust_lexis_nexis_tot_tax_assess_val_am"),
])
def test_adl_name_works_at_transaction_grain(adl, expected):
    """Mirrored into model_scores_transaction.yaml — the same question at
    per-transaction grain must resolve identically."""
    _real_case_tools()
    out = data_tools._summarize_trend_impl(
        table_name="model_scores_transaction", value_column=adl,
        time_column="trans_dt", op="max")
    assert json.loads(out)["value_column"] == expected


def test_adl_name_gives_a_single_value_too():
    _real_case_tools()
    out = data_tools._aggregate_column_impl(
        table_name="model_scores", column="INTOOP", op="max")
    assert "oop_interaction_max" in out and "24.46" in out


def test_search_columns_finds_a_variable_by_its_adl_code():
    _real_case_tools()
    out = data_tools._search_columns_impl("INTOOP")
    assert "oop_interaction" in out


def test_a_name_that_is_not_a_column_says_NOT_FOUND_not_data_gap():
    """The dangerous confusion: an unresolved name used to fall into the DATA
    GAP branch, which says "do NOT retry" — abandoning a real variable over a
    spelling miss. A missing column is a correctable mistake."""
    from agent_factories.agent_tools.grounding import classify_tool_output

    _real_case_tools()
    out = data_tools._summarize_trend_impl(
        table_name="model_scores", value_column="NOSUCHVAR",
        time_column="trans_month", op="max")
    assert "COLUMN NOT FOUND" in out
    assert "DATA GAP" not in out
    assert "search_columns" in out
    # And it must be FLAGGED for retry, unlike a genuine data gap.
    assert classify_tool_output("summarize_trend", out) == "column_not_found"


@pytest.mark.parametrize("table,min_checked", [
    ("model_scores", 10),
    ("model_scores_transaction", 5),
])
def test_every_declared_alias_resolves_to_its_canonical(table, min_checked):
    """Drift guard for declared aliases (ADL codes, CAS names, business names).

    Asserts RESOLUTION, not presence: a real export carries only a subset of
    the catalog (44 of ~250 columns here), so `CUMNTHS2` correctly reports
    COLUMN NOT FOUND on a case whose export lacks `tpf_cust_mod_tenure_max`.
    What must hold is that the catalog can map every declared alias back to its
    canonical — a typo'd alias would resolve to nothing anywhere.

    Covers BOTH grains. The monthly and transaction profiles carry overlapping
    aliases for the same concept at different grains, and an alias added to only
    one of them answers the same question on one table and misses on the other.
    """
    import yaml
    from datalayer.catalog import DataCatalog as _DC

    prof = yaml.safe_load(open(f"config/data_profiles/{table}.yaml"))
    cat = _DC(profile_dir="config/data_profiles")
    suffixes = ("_max", "_min", "_mean")
    checked = 0
    for canon, spec in (prof.get("columns") or {}).items():
        for alias in spec.get("aliases") or []:
            if alias.endswith(suffixes) or alias == canon:
                continue                      # aggregation alias / self-alias
            for spelling in (alias, alias.lower(), alias.upper()):
                got = cat.resolve_real_column(table, spelling, [canon])
                assert got == canon, f"{spelling!r} resolved to {got!r}, want {canon!r}"
            checked += 1
    assert checked >= min_checked, f"expected aliases to be checked, saw {checked}"


def test_exposure_alias_resolves_at_BOTH_grains():
    """`overall_exposure` is the monthly roll-up of the transaction-level
    `one_expsr_usd_currency_amt`; both spellings must land on the right column
    for whichever table the question is asked against."""
    from datalayer.catalog import DataCatalog as _DC

    cat = _DC(profile_dir="config/data_profiles")
    for spelling in ("business_exposure", "overall_exposure", "BUSINESS_EXPOSURE"):
        assert cat.resolve_real_column(
            "model_scores", spelling, ["overall_exposure"]) == "overall_exposure"
        assert cat.resolve_real_column(
            "model_scores_transaction", spelling,
            ["one_expsr_usd_currency_amt"]) == "one_expsr_usd_currency_amt"


def test_an_absent_column_is_reported_even_for_a_valid_alias():
    """`CUMNTHS2` is a real alias whose column is not in this case's export —
    that must read as COLUMN NOT FOUND (correctable), never as a data gap."""
    _real_case_tools()
    out = data_tools._summarize_trend_impl(
        table_name="model_scores", value_column="CUMNTHS2",
        time_column="trans_month", op="max")
    assert "COLUMN NOT FOUND" in out and "DATA GAP" not in out


# ── breach EPISODES, not a flat period list ─────────────────────────────────
#
# Reported: "extract the abnormal transactions when TSR was reacting recently"
# answered with 2024-03..2025-05. That IS the span of the flat breach list on a
# series that breached in 2024, recovered for three months, then breached again
# in 2025-04. "Recently" is the LAST run, and a window is what you filter
# transactions by — a single period or a flat list can't express it.

def test_gapped_breaches_split_into_episodes():
    vals = [(2024, 2, 21.3), (2024, 3, 21.5), (2024, 12, 30.6),
            (2025, 1, 7.4), (2025, 2, 12.8), (2025, 3, 10.4),
            (2025, 4, 27.4), (2025, 5, 20.2), (2025, 6, 7.7)]
    rows = [{"trans_month": f"{y}-{m:02d}-01", "tot_struct_risk_score": v}
            for y, m, v in vals]
    t = _trend_summary(rows)["threshold"]
    assert t["n_episodes"] == 2
    assert (t["episodes"][0]["start"], t["episodes"][0]["end"]) == ("2024-02", "2024-12")
    assert (t["latest_episode"]["start"], t["latest_episode"]["end"]) == ("2025-04", "2025-05")


def test_latest_episode_is_a_window_not_the_whole_breach_span():
    """The exact regression: the answer must not span 2024 → 2025."""
    vals = [(2024, 3, 21.5), (2025, 1, 7.4), (2025, 4, 27.4), (2025, 5, 20.2)]
    rows = [{"trans_month": f"{y}-{m:02d}-01", "tot_struct_risk_score": v}
            for y, m, v in vals]
    t = _trend_summary(rows)["threshold"]
    assert t["latest_episode"]["start"] == "2025-04"      # NOT "2024-03"
    assert t["latest_episode"]["n_periods"] == 2


def test_episode_peak_is_the_peak_within_that_episode():
    """So a recent-episode answer quotes 27.4, not the all-time 39.6."""
    vals = [(2024, 9, 39.6), (2025, 1, 7.4), (2025, 4, 27.4), (2025, 5, 20.2)]
    rows = [{"trans_month": f"{y}-{m:02d}-01", "tot_struct_risk_score": v}
            for y, m, v in vals]
    s = _trend_summary(rows)
    assert s["peak_all_time"]["period"] == "2024-09"
    assert s["threshold"]["latest_episode"]["peak"]["period"] == "2025-04"


def test_below_direction_episodes_track_the_low_side():
    vals = [(2025, 1, 0.80), (2025, 2, 0.31), (2025, 3, 0.90), (2025, 4, 0.20)]
    rows = [{"trans_month": f"{y}-{m:02d}-01", "last_cycle_cut_revolve_rate": v}
            for y, m, v in vals]
    t = _trend_summary(rows, value_column="last_cycle_cut_revolve_rate")["threshold"]
    assert t["n_episodes"] == 2
    assert t["latest_episode"]["start"] == "2025-04"
    assert t["latest_episode"]["peak"]["period"] == "2025-04"   # most extreme LOW


def test_no_breach_has_no_episode():
    rows = [{"trans_month": "2025-01-01", "tot_struct_risk_score": 5.0}]
    t = _trend_summary(rows)["threshold"]
    assert t["n_episodes"] == 0 and t["latest_episode"] is None


def test_query_table_terminates_on_a_wide_table(monkeypatch):
    """Regression: an UNBOUNDED loop, not a slow one.

    The size-truncation loop halved the row count, and `_even_sample` returns
    its input UNCHANGED when max_items < 2 — so once halving reached target=1 it
    handed back every row, re-measured the full size, and spun forever. Fired on
    any table wide enough that 2 rows exceed the char budget with no
    sort_by/limit: `query_table("model_scores")` (44 columns) hung until the
    240s specialist timeout.
    """
    import threading

    # 44 columns of padded values: 2 rows must exceed _MAX_CHARS.
    rows = [{f"c{i}": f"value_{i}_" + "x" * 30 for i in range(44)}
            for _ in range(18)]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))

    box: dict = {}

    def _run():
        box["out"] = data_tools._query_table_impl(table_name="t")

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=15)
    assert not th.is_alive(), "query_table did not terminate — unbounded loop"

    q = json.loads(box["out"])
    assert q["total_rows_in_table"] == 18
    # Termination is what this test is for. The result must also SHRINK — but a
    # small result now shrinks along the column axis, keeping every row: on the
    # monthly tables a row is a month, and halving rows deleted periods out of a
    # series the specialist asked for as a series ("showing 9/18 rows", and at
    # worst 2/18, in the evaluation logs).
    assert q["truncated"] is True
    assert q["rows_returned"] == 18, "rows are periods — trim columns, not rows"
    assert len(q["rows"][0]) < 44, "the result must still shrink to fit"


def test_query_table_still_samples_evenly_when_it_can():
    """The progress guard must not disable even-sampling on the normal path."""
    rows = [{"m": f"M{i:03d}", "amt": float(i)} for i in range(400)]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    q = json.loads(data_tools._query_table_impl(table_name="t"))
    got = [r["m"] for r in q["rows"]]
    assert len(got) > 1
    assert got[0] == "M000" and got[-1] == "M399", "must span the match set"


# ── derived ops: share-of-total and ratio ───────────────────────────────────
#
# So a specialist never divides in its head. Mental math is not usually WRONG,
# but it is untraceable — the reviewer can't tell a measured number from an
# estimated one, and the operand can come from the wrong place even when the
# arithmetic is trivial. Both ops report numerator AND denominator.

def _spend_gw():
    rows = ([{"m": "A", "amt": "100", "risk": "10"}] * 2
            + [{"m": "B", "amt": "300", "risk": "10"}])
    gw = LocalDataGateway(case_data={"C": {"spends": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))


def test_share_is_the_subset_over_the_WHOLE_table():
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="share",
        filter_column="m", filter_value="A")
    assert "40.0%" in out                      # 200 of 500
    # Numerator AND denominator must be visible, or the number isn't checkable.
    assert "200" in out and "500" in out
    assert "2 matching rows" in out and "base = all 3 rows" in out


def test_share_denominator_ignores_the_filter():
    """The whole point — a subset share must not be measured against itself."""
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="share",
        filter_column="m", filter_value="B")
    assert "60.0%" in out                      # 300 of 500, not 300 of 300


def test_share_without_a_filter_is_refused_not_reported_as_100pct():
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="share")
    assert "needs a filter" in out
    assert "100%" in out            # explains WHY, rather than returning it


def test_ratio_is_sum_over_sum():
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="ratio",
        denominator_column="risk")
    assert "16.67" in out           # 500 / 30
    assert "sum-over-sum" in out, "must say which statistic it is"


def test_ratio_respects_the_row_filter():
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="ratio",
        denominator_column="risk", filter_column="m", filter_value="B")
    assert "30" in out              # 300 / 10


def test_ratio_without_a_denominator_column_is_refused():
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="ratio")
    assert "needs `denominator_column`" in out


def test_zero_denominator_is_reported_not_a_crash():
    rows = [{"m": "A", "amt": "100", "risk": "0"}]
    gw = LocalDataGateway(case_data={"C": {"spends": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="ratio", denominator_column="risk")
    assert "undefined" in out and "sums to 0" in out


def test_derived_ops_report_an_unknown_column_as_NOT_FOUND():
    _spend_gw()
    for kwargs in ({"column": "nope", "op": "share", "filter_column": "m",
                    "filter_value": "A"},
                   {"column": "amt", "op": "ratio", "denominator_column": "nope"}):
        out = data_tools._aggregate_column_impl(table_name="spends", **kwargs)
        assert "COLUMN NOT FOUND" in out and "search_columns" in out


def test_existing_ops_are_unchanged():
    """The new branch must not disturb sum/mean/count."""
    _spend_gw()
    assert "500" in data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="sum")
    assert "166" in data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="mean")
    assert "3" in data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="count")


# ── "share of WHAT" is a parameter, not a constant ──────────────────────────

def test_share_base_defaults_to_the_whole_table():
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="share",
        filter_column="m", filter_value="A")
    assert "40.0%" in out and "base = all 3 rows" in out


def test_share_base_can_be_a_subset():
    """"X as a share of MAY" needs May as the base, not the whole year."""
    rows = [{"m": "A", "amt": "100", "mon": "May"},
            {"m": "B", "amt": "100", "mon": "May"},
            {"m": "A", "amt": "800", "mon": "Jun"}]
    gw = LocalDataGateway(case_data={"C": {"spends": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="share",
        filter_column="m", filter_value="A",
        base_filter_column="mon", base_filter_value="May")
    # A-in-May is 100 of May's 200 — NOT 900/1000 against the whole table.
    assert "50.0%" in out
    assert "mon eq 'May'" in out, "the base must be stated, not implied"


def test_share_with_an_empty_base_is_undefined_not_a_number():
    _spend_gw()
    out = data_tools._aggregate_column_impl(
        table_name="spends", column="amt", op="share",
        filter_column="m", filter_value="A",
        base_filter_column="m", base_filter_value="ZZZ")
    assert "undefined" in out and "matched no rows" in out


def test_group_rows_carry_both_share_denominators():
    """"top 1 of spend" and "top 1 among the top 5" are different answers to
    different questions; both must be readable without dividing."""
    rows = [{"m": "A", "amt": 50.0}, {"m": "B", "amt": 30.0},
            {"m": "C", "amt": 20.0}, {"m": "D", "amt": 100.0}]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    g = json.loads(data_tools._summarize_by_group_impl(
        table_name="t", value_column="amt", group_column="m",
        op="sum", top_n=2))
    top = g["groups"][0]
    assert top["share_of_total"] == "50.0%"     # 100 of 200
    assert top["share_of_shown"] == "66.7%"     # 100 of the shown 150


def test_share_of_shown_is_omitted_when_nothing_was_truncated():
    """Otherwise it just repeats share_of_total and adds noise."""
    rows = [{"m": "A", "amt": 50.0}, {"m": "B", "amt": 50.0}]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    g = json.loads(data_tools._summarize_by_group_impl(
        table_name="t", value_column="amt", group_column="m",
        op="sum", top_n=10))
    assert "share_of_shown" not in g["groups"][0]
    assert g["groups"][0]["share_of_total"] == "50.0%"


def test_group_shares_are_omitted_for_non_additive_ops():
    rows = [{"m": "A", "amt": 50.0}, {"m": "B", "amt": 30.0}]
    gw = LocalDataGateway(case_data={"C": {"t": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))
    g = json.loads(data_tools._summarize_by_group_impl(
        table_name="t", value_column="amt", group_column="m",
        op="mean", top_n=10))
    assert "share_of_total" not in g["groups"][0]


# ── search_columns: the two shapes that burned a specialist's turn budget ────
#
# Observed run (modeling, 2026-07-30): the sub-question said "for any months
# flagged by spend_payments…", naming a SPECIALIST. modeling read it as a table
# and spent turns 1-2 discovering it wasn't one, then turns 3-4 rediscovering
# columns search_columns should have found — exhausting a 6-turn budget and
# surfacing to the reviewer as "No model-based feature analysis is available due
# to a data retrieval issue."
#
# Neither failure was a data problem. Both were tool OUTPUT SHAPES: one gave
# misleading advice, the other gave a false negative.


def test_unknown_table_is_not_reported_as_no_columns_matched():
    """"no columns matched. Try a broader term" sent the specialist to rephrase
    a query that was never the problem. Name the real fault and the way out."""
    _real_case_tools()
    out = data_tools._search_columns_impl(
        query="spend spike", table_name="spend_payments", limit=25)

    assert "not found for current case" in out
    assert "no columns matched" not in out
    assert "broader term" not in out, "the query was fine; the TABLE was wrong"
    # The way out has to be in the message — a name it can actually pass next.
    assert "spends_data" in out and "Available tables:" in out


def test_an_unknown_table_from_search_is_benign_not_a_failed_call():
    """Discovery is how a specialist finds its footing. Probing a name that
    turns out not to be a table must not flag the run as ungrounded — the same
    over-flagging that once quarantined an honest DATA GAP report."""
    from agent_factories.agent_tools.grounding import classify_tool_output

    _real_case_tools()
    out = data_tools._search_columns_impl(query="x", table_name="spend_payments")
    assert classify_tool_output("search_columns", out) is None


def test_a_real_table_still_searches_normally():
    """The unknown-table branch must not swallow the ordinary case."""
    _real_case_tools()
    out = data_tools._search_columns_impl(query="spend", table_name="model_scores")
    assert "not found for current case" not in out
    assert "matches" in out or "match" in out


@pytest.mark.parametrize("asked,expected_column", [
    ("spending", "cust_enhnc_one_way_spend_concentration_30day_rt1_max"),
    ("payments", "payments"),          # any payment-named column
    ("scores", "score"),
])
def test_question_form_finds_column_form(asked, expected_column):
    """A question says "spending"; the column says "spend". Exact-token search
    returned NOTHING for the very columns it exists to surface."""
    _real_case_tools()
    out = data_tools._search_columns_impl(query=asked, limit=25)
    assert "no columns matched" not in out, f"{asked!r} found nothing"
    assert expected_column.split("_")[0] in out.lower()


def test_stem_never_shortens_below_the_substring_floor():
    """Stemming must not undo `_SEARCH_MIN_SUBSTRING_TERM`. A 2-3 char stem
    substring-matches half the catalog, which is the noise that floor exists
    to stop."""
    from tools.data_tools import _SEARCH_MIN_STEM, _stem

    assert _stem("spending") == "spend"
    assert _stem("payments") == "payment"
    for junk in ("uses", "ties", "access", "is", "was", "ss"):
        assert _stem(junk) == "" or len(_stem(junk)) >= _SEARCH_MIN_STEM


def test_stemming_did_not_flatten_the_relevance_ranking():
    """Widening the net is only safe if identifier matches still outrank
    description-only ones — the specialist reads top-first."""
    _real_case_tools()
    entries = data_tools._build_search_index(_REAL_CASE)
    terms = data_tools._search_tokens("spending")
    qn = data_tools._normalize("spending")
    scored = sorted(
        ((data_tools._score_entry(e, terms, qn), e) for e in entries),
        key=lambda se: -se[0])
    top_score, top_entry = scored[0]
    assert "spend" in top_entry["column"].lower()
    # A column matched only via its description must not tie the best hit.
    desc_only = [s for s, e in scored
                 if s > 0 and "spend" not in e["column"].lower()
                 and not any("spend" in c for c in e["concepts"])]
    assert not desc_only or max(desc_only) < top_score


# ── the case's column inventory, handed to specialists up front ──────────────
#
# `tool_choice="required"` makes the specialist's FIRST move a tool call. With
# nothing in its prompt describing this case, that first move is a blind probe:
# `modeling` spent four of six turns on list/search/schema round-trips and ran
# out of budget, which the reviewer saw as "No model-based feature analysis is
# available due to a data retrieval issue."


def test_inventory_lists_real_column_names_verbatim():
    _real_case_tools()
    inv = data_tools.build_column_inventory(["model_scores"])
    # The REAL name, not the canonical profile name — this is what tools accept.
    assert "modelling_data" in inv
    assert "cust_enhnc_one_way_spend_concentration_30day_rt1_max" in inv


def test_inventory_carries_the_alias_spellings_a_reviewer_types():
    """The point of aliases here is RECOGNITION, not resolution — the catalog
    already resolves INTOOP. Without it in the inventory a specialist can decide
    the variable does not exist and report a data gap for a live column."""
    _real_case_tools()
    inv = data_tools.build_column_inventory(["model_scores"])
    assert "=INTOOP" in inv
    assert "=CUIDINDX" in inv


def test_inventory_omits_aliases_that_add_nothing():
    """148 of 194 declared aliases are the column's own name or its
    `_max`/`_min`/`_mean` form. Listing them triples the alias budget to repeat
    what is already on the line."""
    _real_case_tools()
    inv = data_tools.build_column_inventory(["model_scores"])
    assert "=oop_interaction_max" not in inv
    assert "=cust_rnn_score_max" not in inv


def test_inventory_marks_columns_that_are_empty_for_this_case():
    """Present-but-blank is the difference between a usable column and a
    guaranteed DATA GAP. `cust_sbfe_score_min` is all-blank in this case."""
    _real_case_tools()
    inv = data_tools.build_column_inventory(["model_scores"])
    line = next(l for l in inv.splitlines() if "cust_sbfe_score_min" in l)
    assert "(EMPTY)" in line
    # A column WITH data must not be marked.
    other = next(l for l in inv.splitlines() if "credit_loss_prob_max" in l)
    assert "(EMPTY)" not in other


def test_inventory_scopes_to_the_tables_asked_for():
    _real_case_tools()
    inv = data_tools.build_column_inventory(["spends"])
    assert "spends_data" in inv
    assert "modelling_data" not in inv, "a specialist should not pay for other domains"


def test_inventory_is_empty_without_a_data_layer():
    """Callers append it unconditionally, so it must degrade to nothing."""
    data_tools.init_tools(None, None)
    assert data_tools.build_column_inventory(["model_scores"]) == ""


def test_inventory_does_not_survive_a_case_switch():
    """A stale inventory would describe the PREVIOUS case's data — the one
    mistake this must never make."""
    _real_case_tools()
    first = data_tools.build_column_inventory(["model_scores"])
    assert first
    data_tools.clear_schema_cache()
    data_tools.init_tools(None, None)
    assert data_tools.build_column_inventory(["model_scores"]) == ""


@pytest.mark.parametrize("profile", [
    "model_scores", "model_scores_transaction",
    "score_drivers", "score_drivers_transaction",
])
def test_short_descriptions_stay_short_and_do_not_restate_thresholds(profile):
    """`short_description` answers "is this the column I want?"; the full
    `description` answers "how do I read it?". A short one that restates the
    structured threshold is duplication — it is already rendered as
    `(risky > 2.4)` from risk_threshold/risk_direction."""
    import re as _re
    import yaml

    prof = yaml.safe_load(open(f"config/data_profiles/{profile}.yaml"))
    thresh = _re.compile(r"(above|below)\s+[-0-9.]+\s+(are|is)\s+(considered\s+)?risky",
                         _re.I)
    for col, spec in (prof.get("columns") or {}).items():
        s = (spec or {}).get("short_description")
        if not s:
            continue
        assert len(s) <= data_tools._INVENTORY_MAX_SHORT, f"{col}: {len(s)} chars"
        assert not thresh.search(s), f"{col} restates its risk_threshold"
        assert ".g." not in s, f"{col} looks truncated mid-abbreviation: {s!r}"


# ── ground truth for the screen: does this text name a real variable? ────────


def test_known_variables_matches_an_adl_code_in_any_case():
    _real_case_tools()
    for spelling in ("intoop", "INTOOP", "IntOop"):
        assert data_tools.known_variables_in(f"how is {spelling}") == \
            ["oop_interaction_max"]


def test_known_variables_matches_the_cas_name_despite_the_agg_suffix():
    """A reviewer types `oop_interaction`; the case stores
    `oop_interaction_max`. The `_max`/`_min`/`_mean` suffix is an aggregation
    artefact, not part of the name they know."""
    _real_case_tools()
    assert data_tools.known_variables_in("trend of oop_interaction") == \
        ["oop_interaction_max"]


@pytest.mark.parametrize("question", [
    "what is the date today",
    "how much does it amount to",
    "what to eat for lunch",
    "tell me a joke",
    "what is the total balance",
])
def test_known_variables_ignores_ordinary_english(question):
    """`Date`, `Amount`, `Month`, `Balance` are real columns. This function
    OVERRIDES a screen rejection, so a loose match here would wave through
    anything."""
    _real_case_tools()
    assert data_tools.known_variables_in(question) == []


def test_known_variables_is_quiet_without_a_data_layer():
    data_tools.init_tools(None, None)
    assert data_tools.known_variables_in("how is intoop") == []


# ── every quoted number carries the record count behind it ──────────────────
#
# Reported: answers cite amounts with no transaction count. "$404,152 in May
# 2025" reads as a spending surge whether it came from 1,200 transactions or 3,
# and the count is what a case reviewer needs to tell those apart. The counts
# were already in `series`; the SUMMARY landmarks a specialist actually quotes
# omitted them, so reaching one meant cross-referencing by period.


@pytest.mark.parametrize("landmark", ["first", "last", "peak_all_time",
                                      "trough_all_time"])
def test_trend_landmarks_carry_their_record_count(landmark):
    _real_case_tools()
    d = json.loads(data_tools._summarize_trend_impl(
        table_name="spends", value_column="Amount",
        time_column="Date", period="month", op="sum"))
    mark = d["summary"][landmark]
    assert "n_records" in mark, f"{landmark} quotes a value with no count"
    assert isinstance(mark["n_records"], int) and mark["n_records"] > 0


def test_landmark_counts_agree_with_the_series():
    """The count must be the bucket's own, not a total or a placeholder."""
    _real_case_tools()
    d = json.loads(data_tools._summarize_trend_impl(
        table_name="spends", value_column="Amount",
        time_column="Date", period="month", op="sum"))
    by_period = {s["period"]: s["n_records"] for s in d["series"]}
    for landmark in ("first", "last", "peak_all_time", "trough_all_time"):
        mark = d["summary"][landmark]
        assert mark["n_records"] == by_period[mark["period"]]


def test_a_partial_boundary_month_is_legible_from_its_count():
    """This case's last bucket is a mid-month cut-off: 22 records against 682
    at the peak. Without the count, the drop reads as collapsed spending
    rather than a truncated month."""
    _real_case_tools()
    d = json.loads(data_tools._summarize_trend_impl(
        table_name="spends", value_column="Amount",
        time_column="Date", period="month", op="sum"))
    assert d["summary"]["last"]["n_records"] < \
        d["summary"]["peak_all_time"]["n_records"] / 4


def test_breach_episodes_carry_counts_across_the_window():
    """An episode window gets quoted and used as a date filter. A three-month
    breach spanning 12 records is a different finding from one spanning 1,200."""
    _real_case_tools()
    d = json.loads(data_tools._summarize_trend_impl(
        table_name="model_scores", value_column="tot_struct_risk_score_max",
        time_column="trans_month", period="month", op="max"))
    thr = d["summary"]["threshold"]
    assert "n_breaching_records" in thr
    for ep in thr["episodes"]:
        assert "n_records" in ep and "n_records" in ep["peak"]
    assert thr["latest_breach"]["n_records"] >= 1
    # The episode total must be the sum of its periods, not a period count.
    assert thr["latest_episode"]["n_records"] >= thr["latest_episode"]["n_periods"]


def test_query_table_keeps_every_month_of_a_monthly_table():
    """A row on the monthly tables IS a month — never sample them away.

    From the evaluation logs: `query_table("score_drivers_data")` reported
    "showing 9/18 rows", and elsewhere "showing 2/18 rows". The specialist then
    narrated an 18-month trajectory from a quarter of it. Columns are the safe
    axis to shed — the caller can re-request a column by name, but cannot
    re-request a month it was never shown.
    """
    rows = [
        {"trans_month": f"2024-{m:02d}" if m <= 12 else f"2025-{m - 12:02d}",
         "top_cdss1": "delinquency_severity_index_recent_24m",
         "top_cdss2": "utilization_trailing_six_month_average",
         "top_cdss3": "external_inquiry_velocity_90d",
         "top_tsr1": "structural_risk_balance_growth_ratio",
         "top_tsr2": "merchant_concentration_top3_share",
         "top_tsr3": "payment_shortfall_persistence_score"}
        for m in range(1, 19)
    ]
    gw = LocalDataGateway(case_data={"C": {"score_drivers_data": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))

    q = json.loads(data_tools._query_table_impl(table_name="score_drivers_data"))
    assert q["rows_returned"] == 18, "all 18 months must survive"
    months = [r["trans_month"] for r in q["rows"] if "trans_month" in r]
    assert months == sorted(months) and len(months) == 18


def test_query_table_still_samples_a_genuinely_large_result():
    """The row-sampling path must stay intact — it is what stops a 3865-row dump."""
    rows = [{"txn_id": i, "amount": 100 + i, "merchant": f"M{i % 7}_" + "x" * 40}
            for i in range(3865)]
    gw = LocalDataGateway(case_data={"C": {"spends_data": rows}})
    gw.set_case("C")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))

    q = json.loads(data_tools._query_table_impl(table_name="spends_data"))
    assert q["total_rows_in_table"] == 3865
    assert q["rows_returned"] < 3865 and q["truncated"] is True


# ── zero-match: which condition zeroed a conjunction ────────────────────────
#
# A conjunction empties if ANY ONE condition is wrong, and the per-column
# vocabulary hints look healthy while it happens — every column exists, every
# column holds plausible values. Observed live: "no spend transactions with
# high TSR (>20), large amounts, or flagged outlier patterns were found" on a
# case that had them, because a threshold from MONTHLY grain was applied to
# transaction rows.

def _txn_rows():
    return [
        {"trans_dt": "2024-09-14", "tot_struct_risk_score": "8.2", "amount": "14500"},
        {"trans_dt": "2024-09-21", "tot_struct_risk_score": "9.1", "amount": "22000"},
        {"trans_dt": "2025-01-08", "tot_struct_risk_score": "7.4", "amount": "18000"},
    ]


def test_zero_match_names_the_condition_that_zeroed_the_conjunction():
    from tools.data_tools import _zero_match_diagnostic

    d = _zero_match_diagnostic(_txn_rows(), [
        ("trans_dt", "2024-09-01,2024-09-30", "between"),
        ("tot_struct_risk_score", "20", "gte"),   # the culprit
        ("amount", "10000", "gt"),
    ])
    alone = d["_conditions_alone"]
    counts = alone["rows_matching_each_condition_by_itself"]

    assert counts["tot_struct_risk_score gte 20"] == 0
    assert counts["trans_dt between 2024-09-01,2024-09-30"] == 2
    assert counts["amount gt 10000"] == 3
    # The hint must name the culprit AND forbid the false-negative reading.
    assert "tot_struct_risk_score gte 20" in alone["hint"]
    assert "no such rows exist" in alone["hint"]


def test_zero_match_calls_a_real_negative_a_real_negative():
    """Every condition matches alone but the AND does not — the emptiness is
    the combination, which IS a finding and must not be blamed on a filter."""
    from tools.data_tools import _zero_match_diagnostic

    d = _zero_match_diagnostic(_txn_rows(), [
        ("trans_dt", "2025-01-01,2025-01-31", "between"),
        ("amount", "20000", "gt"),
    ])
    alone = d["_conditions_alone"]

    assert all(n > 0 for n in alone["rows_matching_each_condition_by_itself"].values())
    assert "real negative" in alone["hint"]
    assert alone["total_rows_before_filtering"] == 3


def test_zero_match_skips_attribution_for_a_single_condition():
    """With one condition there is nothing to attribute — no noise block."""
    from tools.data_tools import _zero_match_diagnostic

    d = _zero_match_diagnostic(_txn_rows(), [("amount", "999999", "gt")])
    assert "_conditions_alone" not in (d or {})


def test_missing_column_is_never_read_as_a_real_negative():
    """The live failure's actual shape, and the one that must not regress.

    `spends` carries Amount / Merchant Name but no model scores;
    `model_scores_transaction` carries tot_struct_risk_score but no amount or
    merchant. Filtering one by the other's column returns 0 rows and looks
    exactly like absence. A missing column OUTRANKS the per-condition counts:
    the surviving conditions matching on their own says nothing, because the
    missing one was never tested at all.
    """
    from tools.data_tools import _zero_match_diagnostic

    d = _zero_match_diagnostic(_txn_rows(), [
        ("no_such_column", "1", "eq"),
        ("amount", "10000", "gt"),          # matches on its own
    ])
    assert d["no_such_column"]["column_exists"] is False
    block = d["_conditions_alone"]
    # Not attributed a count — it was never evaluated.
    assert "no_such_column eq 1" not in block["rows_matching_each_condition_by_itself"]
    # And crucially, NOT reported as a real negative.
    assert block["testable"] is False
    assert block["columns_not_on_this_table"] == ["no_such_column"]
    assert "NOT A NEGATIVE FINDING" in block["hint"]
    assert "real negative" not in block["hint"]
    assert "transaction_detail" in block["hint"]


# ── join_table: small results lose COLUMNS, never ROWS ──────────────────────
#
# A join is where this bites hardest: the whole point is to widen rows by
# gluing two tables together, so the output is small-but-wide by construction.
# Measured before the fix — joining `bureau` to `model_scores` on the month key
# matched all 18 months and returned 9, silently dropping every other month of
# a series someone joined precisely to compare month by month.

def _wide_monthly_pair(n_rows=18, n_cols=30):
    """Two monthly tables that join 1:1 and are wide enough to bust the budget."""
    left, right = [], []
    for i in range(n_rows):
        # UNIQUE months — a repeated key would fan out into a many-to-many
        # join and `matched_rows` would exceed the row count.
        m = f"{2024 + i // 12}-{i % 12 + 1:02d}"
        left.append({"month": m, "FICO Score": str(600 + i),
                     **{f"L{c}": "x" * 60 for c in range(n_cols)}})
        right.append({"trans_month": m, "cbr_score_max": str(600 + i),
                      **{f"R{c}": "y" * 60 for c in range(n_cols)}})
    return left, right


def _join(monkeypatch, left, right, **kw):
    class _GW:
        def query(self, table, filters=None):
            return {"L": [dict(r) for r in left], "R": [dict(r) for r in right]}.get(table)
    monkeypatch.setattr(data_tools, "_gateway", _GW())
    monkeypatch.setattr(data_tools, "_resolve_real_table", lambda t: t)
    kw.setdefault("left_on", "month")
    kw.setdefault("right_on", "trans_month")
    return json.loads(data_tools._join_table_impl(left_table="L", right_table="R", **kw))


def test_join_keeps_every_row_on_a_small_wide_result(monkeypatch):
    left, right = _wide_monthly_pair()
    r = _join(monkeypatch, left, right)

    assert r["matched_rows"] == 18
    assert r["rows_returned"] == 18, "a monthly series must not lose periods"
    assert len(r["rows"][0]) < 62, "columns should have been shed instead"
    assert "columns to keep all 18 joined rows" in r["truncation_note"]


def test_join_never_drops_the_join_key(monkeypatch):
    """A key sitting late in the column order would otherwise be shed first,
    leaving rows nobody can line up against either source table."""
    left, right = _wide_monthly_pair()
    # Push the key to the END of each row's column order.
    left = [{**{k: v for k, v in r.items() if k != "month"}, "month": r["month"]}
            for r in left]
    right = [{**{k: v for k, v in r.items() if k != "trans_month"},
              "trans_month": r["trans_month"]} for r in right]

    r = _join(monkeypatch, left, right)

    assert r["rows_returned"] == 18
    assert "month" in r["rows"][0]


def test_join_leaves_a_result_that_already_fits_alone(monkeypatch):
    left, right = _wide_monthly_pair(n_rows=18, n_cols=0)
    r = _join(monkeypatch, left, right)

    assert r["rows_returned"] == 18
    assert r["truncated"] is False
    assert set(r["rows"][0]) == {"month", "FICO Score", "trans_month", "cbr_score_max"}


def test_join_still_sheds_rows_on_a_genuinely_large_result(monkeypatch):
    """Above the small-result threshold, sampling rows stays legitimate."""
    left, right = _wide_monthly_pair(n_rows=400, n_cols=20)
    r = _join(monkeypatch, left, right)

    assert r["matched_rows"] == 400
    assert r["rows_returned"] < 400
    assert "joined rows" in r["truncation_note"]


# ── aggregate_column: ABSENT column vs EMPTY column ────────────────────────
#
# Two states, two answers. `summarize_trend` already split them and its comment
# explains why conflating them is actively harmful: the DATA GAP wording says
# "do NOT retry", so a name miss would make the specialist abandon a variable
# that exists; the "check the column name" wording sends it chasing a name that
# was already correct. `aggregate_column` used to return the SAME string for
# both — measured on `bureau_data.SBFE Score` (26 rows, all blank) versus a typo
# of that name.

def _blank_col_rows():
    return [{"month": f"2024-{i+1:02d}", "SBFE Score": "", "FICO Score": str(700 + i)}
            for i in range(6)]


def _agg(monkeypatch, **kw):
    rows = _blank_col_rows()

    class _GW:
        def query(self, table, filters=None):
            return [dict(r) for r in rows] if table == "T" else None

    monkeypatch.setattr(data_tools, "_gateway", _GW())
    monkeypatch.setattr(data_tools, "_resolve_real_table", lambda t: t)
    kw.setdefault("table_name", "T")
    return data_tools._aggregate_column_impl(**kw)


@pytest.mark.parametrize("op", ["mean", "max", "min", "sum"])
def test_aggregate_empty_column_is_a_data_gap_not_a_name_problem(monkeypatch, op):
    out = _agg(monkeypatch, column="SBFE Score", op=op)

    assert "DATA GAP" in out
    assert "EMPTY for this case" in out
    assert "Record it as a data_gap" in out
    # Must NOT send the specialist off to fix a name that is already right.
    assert "search_columns" not in out
    assert "COLUMN NOT FOUND" not in out


@pytest.mark.parametrize("op", ["mean", "max", "sum"])
def test_aggregate_absent_column_is_a_mistake_not_a_data_gap(monkeypatch, op):
    out = _agg(monkeypatch, column="SBFE_Scoree", op=op)

    assert "COLUMN NOT FOUND" in out
    assert "search_columns" in out
    assert "Do NOT report this as a data gap" in out
    assert "DATA GAP:" not in out


def test_count_on_an_all_blank_column_says_it_counted_rows(monkeypatch):
    """`count` is a ROW count. On a blank column that reads as a data volume it
    is not — "how many SBFE scores?" would be answered with the row count."""
    out = _agg(monkeypatch, column="SBFE Score", op="count")

    assert "count = 6" in out
    assert "counts ROWS, not values" in out
    assert "DATA GAP" in out


def test_count_on_a_populated_column_is_unchanged(monkeypatch):
    out = _agg(monkeypatch, column="FICO Score", op="count")

    assert "count = 6" in out
    assert "DATA GAP" not in out
    assert "counts ROWS" not in out
