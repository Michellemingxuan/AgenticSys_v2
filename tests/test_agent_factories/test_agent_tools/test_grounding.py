"""Tests for grounding.scan_tool_errors — deterministic detection of specialist
runs that rested on a tool call which failed and was never re-issued."""
import pytest

import json

from agent_factories.agent_tools.grounding import (
    classify_tool_output,
    classify_tool_output_detailed,
    scan_tool_errors,
)


class _Result:
    """Stand-in for the SDK RunResult: only to_input_list() is read."""

    def __init__(self, items):
        self._items = items

    def to_input_list(self):
        return self._items


def _call(call_id, name):
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": "{}"}


def _output(call_id, text):
    return {"type": "function_call_output", "call_id": call_id, "output": text}


# ── classify_tool_output ────────────────────────────────────────────────────

def test_classify_specs_unparseable():
    out = ("batch_summarize_trend received a specs_json that was malformed: '['. "
           "batch_summarize_trend did NOT run — you have NO data from it.")
    assert classify_tool_output("batch_summarize_trend", out) == "specs_unparseable"


def test_classify_no_parseable_values_splits_by_tool():
    trend = "trend(max(x) by month on trans_month) = (no parseable trans_month values; 18 total)"
    group = "top_groups(sum(Amount) by Merchant Industry) = (no parseable values; 8,888 total)"
    assert classify_tool_output("summarize_trend", trend) == "no_buckets"
    assert classify_tool_output("summarize_by_group", group) == "no_groups"


def test_classify_data_layer_uninitialized():
    bare = "Data unavailable"
    verbose = ("Data unavailable: data layer is not initialized for this session "
               "(no gateway is bound to tools.data_tools).")
    assert classify_tool_output("list_available_tables", bare) == "data_layer_uninitialized"
    assert classify_tool_output("query_table", verbose) == "data_layer_uninitialized"


def test_classify_bare_data_unavailable_from_schema_tool_is_still_flagged():
    """data_tools.py:1006 / :1089 — get_table_schema with a None catalog is a dead
    data layer, NOT a benign probe miss. The carve-out must not swallow it."""
    assert classify_tool_output("get_table_schema", "Data unavailable") == \
        "data_layer_uninitialized"


@pytest.mark.parametrize("tool", [
    "query_table", "join_table", "transaction_detail",
    "aggregate_column", "summarize_trend", "summarize_by_group",
])
def test_classify_table_not_found_is_flagged_for_data_tools(tool):
    out = "Data unavailable: table 'wcc' not found for current case."
    assert classify_tool_output(tool, out) == "table_not_found"


def test_classify_table_not_found_is_benign_for_schema_probe():
    """data_tools.py:1014 — the ONE benign site. Schema probing is normal
    exploration; the specialist learns the table is absent and picks another."""
    out = "Data unavailable: table 'wcc' not found for current case."
    assert classify_tool_output("get_table_schema", out) is None


def test_classify_transaction_detail_base_table_variant():
    out = "Data unavailable: base table 'spends' not found for current case."
    assert classify_tool_output("transaction_detail", out) == "table_not_found"


def test_classify_batch_element_error():
    out = '{"results": [{"index": 0, "error": "spec must be an object"}]}'
    assert classify_tool_output("batch_aggregate", out) == "spec_rejected"


def test_classify_clean_output_returns_none():
    ok = '{"table": "modelling_data", "series": [{"period": "2025-01", "value": "3"}]}'
    assert classify_tool_output("summarize_trend", ok) is None


def test_classify_zero_rows_is_not_an_error():
    """An honest empty result. Flagging it would fire on most narrow questions."""
    out = '{"table": "spends_data", "rows_matching_filter": 0, "rows": []}'
    assert classify_tool_output("query_table", out) is None


# ── scan_tool_errors ────────────────────────────────────────────────────────

def test_scan_empty_run_returns_empty():
    assert scan_tool_errors(_Result([])) == []


def test_scan_flags_unrecovered_error():
    r = _Result([
        _call("c1", "batch_summarize_trend"),
        _output("c1", "batch_summarize_trend did NOT run — you have NO data from it."),
    ])
    found = scan_tool_errors(r)
    assert len(found) == 1
    assert found[0]["tool"] == "batch_summarize_trend"
    assert found[0]["reason"] == "specs_unparseable"
    assert found[0]["call_id"] == "c1"


def test_scan_drops_error_superseded_by_later_clean_call_to_same_tool():
    """The specialist noticed its malformed JSON and re-issued correctly. That is
    the DESIRED behavior and must not be punished."""
    r = _Result([
        _call("c1", "batch_summarize_trend"),
        _output("c1", "batch_summarize_trend did NOT run — you have NO data from it."),
        _call("c2", "batch_summarize_trend"),
        _output("c2", '{"results": [{"index": 0, "result": "{\\"series\\": []}"}]}'),
    ])
    assert scan_tool_errors(r) == []


def test_scan_keeps_error_when_later_clean_call_is_a_different_tool():
    r = _Result([
        _call("c1", "summarize_trend"),
        _output("c1", "trend(max(x) by month on trans_month) = (no parseable trans_month values; 18 total)"),
        _call("c2", "query_table"),
        _output("c2", '{"table": "spends_data", "rows_matching_filter": 3, "rows": []}'),
    ])
    found = scan_tool_errors(r)
    assert [f["tool"] for f in found] == ["summarize_trend"]
    assert found[0]["reason"] == "no_buckets"


def test_scan_keeps_error_when_the_retry_also_failed():
    r = _Result([
        _call("c1", "summarize_trend"),
        _output("c1", "trend(...) = (no parseable trans_month values; 18 total)"),
        _call("c2", "summarize_trend"),
        _output("c2", "trend(...) = (no parseable trans_month values; 18 total)"),
    ])
    assert len(scan_tool_errors(r)) == 1


def test_scan_pairs_by_call_id_not_position():
    """Parallel tool calls interleave; outputs must bind to their own call."""
    r = _Result([
        _call("c1", "summarize_trend"),
        _call("c2", "query_table"),
        _output("c2", '{"table": "spends_data", "rows_matching_filter": 3, "rows": []}'),
        _output("c1", "trend(...) = (no parseable trans_month values; 18 total)"),
    ])
    found = scan_tool_errors(r)
    assert [f["tool"] for f in found] == ["summarize_trend"]


def test_scan_tolerates_missing_call_id():
    """Defensive: fall back to the most recent unmatched call rather than crash."""
    r = _Result([
        {"type": "function_call", "name": "summarize_trend", "arguments": "{}"},
        {"type": "function_call_output",
         "output": "trend(...) = (no parseable trans_month values; 18 total)"},
    ])
    found = scan_tool_errors(r)
    assert len(found) == 1
    assert found[0]["tool"] == "summarize_trend"


def test_scan_returns_empty_when_result_has_no_to_input_list():
    assert scan_tool_errors(object()) == []


def test_scan_excerpt_is_bounded():
    r = _Result([
        _call("c1", "summarize_trend"),
        _output("c1", "no parseable trans_month values " + "x" * 5000),
    ])
    assert len(scan_tool_errors(r)[0]["excerpt"]) <= 300


def test_scan_reports_the_final_failure_after_an_intervening_success():
    """Supersession is ORDER-SENSITIVE. A clean call clears only the failures
    BEFORE it — a run whose final call returned nothing is not exonerated by an
    earlier success, which is the whole fabrication path this module catches."""
    err = "trend(...) = (no parseable trans_month values; 18 total)"
    ok = '{"table": "m", "series": [{"period": "2025-01", "raw_value": 3}]}'
    r = _Result([
        _call("c1", "summarize_trend"), _output("c1", err),
        _call("c2", "summarize_trend"), _output("c2", ok),
        _call("c3", "summarize_trend"), _output("c3", err),
    ])
    found = scan_tool_errors(r)
    assert [f["call_id"] for f in found] == ["c3"]


def test_scan_reports_the_latest_reason_when_a_tool_fails_twice():
    """The reported reason must describe the run's CURRENT state — the retry
    directive and the payload banner quote it back to the reader."""
    r = _Result([
        _call("a1", "batch_summarize_trend"),
        _output("a1", "batch_summarize_trend did NOT run — you have NO data from it."),
        _call("a2", "batch_summarize_trend"),
        _output("a2", "(no parseable trans_month values; 3 total)"),
    ])
    found = scan_tool_errors(r)
    assert [(f["call_id"], f["reason"]) for f in found] == [("a2", "no_buckets")]


def test_scan_reports_at_most_one_entry_per_tool():
    err = "trend(...) = (no parseable trans_month values; 18 total)"
    r = _Result([
        _call("c1", "summarize_trend"), _output("c1", err),
        _call("c2", "summarize_trend"), _output("c2", err),
        _call("c3", "summarize_trend"), _output("c3", err),
    ])
    assert len(scan_tool_errors(r)) == 1


# ── partial batch failures ──────────────────────────────────────────────────
#
# Regression: a batch tool returns one result per spec, and a single bad spec
# used to condemn the whole call. Seen in prod on case 366132845011 —
# `bureau_data` carries all-blank columns (SBFE Score and friends), so a trend
# over one legitimately reports "no parseable values" (a DATA GAP, the tool
# worked), and that one element quarantined bureau's perfectly good FICO and
# delinquency numbers. general_specialist then relayed "bureau's FICO scores
# and delinquency counts may be unreliable" to the reviewer.

_OK_TREND = '{"table": "bureau_data", "series": [{"period": "2023-07", "value": 654}]}'
_BAD_TREND = ('trend(max(SBFE Score) by month on month) = '
              '(no parseable SBFE Score values; 26 total)')


def _batch(*inners):
    return json.dumps({"results": [
        {"index": i, "value_column": f"c{i}", "result": inner}
        for i, inner in enumerate(inners)
    ]}, indent=2)


def test_partial_batch_is_flagged_but_marked_partial():
    d = classify_tool_output_detailed(
        "batch_summarize_trend", _batch(_OK_TREND, _OK_TREND, _BAD_TREND))
    assert d["reason"] == "no_buckets"
    assert d["partial"] is True
    assert (d["n_failed"], d["n_total"]) == (1, 3)


def test_batch_where_every_spec_failed_is_NOT_partial():
    d = classify_tool_output_detailed(
        "batch_summarize_trend", _batch(_BAD_TREND, _BAD_TREND))
    assert d["partial"] is False
    assert (d["n_failed"], d["n_total"]) == (2, 2)


def test_fully_successful_batch_is_clean():
    assert classify_tool_output_detailed(
        "batch_summarize_trend", _batch(_OK_TREND, _OK_TREND)) is None


def test_batch_that_never_ran_is_a_total_failure():
    """A malformed specs_json is emitted as a bare string, not a results list —
    nothing ran, so it must not be softened into a partial."""
    out = ("batch_summarize_trend received a specs_json that was malformed: "
           "'['. batch_summarize_trend did NOT run — you have NO data from it.")
    d = classify_tool_output_detailed("batch_summarize_trend", out)
    assert d["reason"] == "specs_unparseable" and d["partial"] is False


def test_scalar_outputs_are_never_partial():
    d = classify_tool_output_detailed("summarize_trend", _BAD_TREND)
    assert d["partial"] is False and (d["n_failed"], d["n_total"]) == (1, 1)


def test_scan_carries_partial_through_to_the_caller():
    r = _Result([_call("c1", "batch_summarize_trend"),
                 _output("c1", _batch(_OK_TREND, _BAD_TREND))])
    err = scan_tool_errors(r)[0]
    assert err["partial"] is True
    assert (err["n_failed"], err["n_total"]) == (1, 2)
