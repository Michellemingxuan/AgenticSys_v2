"""Claim audit: do the answer's numbers trace to the tool outputs?

SHADOW MODE — these pin what it REPORTS, not any behavior change. The
false-negative tests matter as much as the positives: an auditor that flags
legitimate answers is worse than none, and this system has over-flagged twice
(the bureau partial-batch false positive, and a DATA GAP branch that told
specialists to abandon a real column).
"""
import json

import pytest

from agent_factories.agent_tools.claim_audit import (
    _numbers_in,
    audit_claims,
)
from models.types import SpecialistOutput


class _Result:
    def __init__(self, outputs):
        self._items = []
        for i, out in enumerate(outputs):
            cid = f"c{i}"
            self._items.append({"type": "function_call", "call_id": cid,
                                "name": "query_table", "arguments": "{}"})
            self._items.append({"type": "function_call_output",
                                "call_id": cid, "output": out})

    def to_input_list(self):
        return list(self._items)


def _out(findings, evidence=None):
    return SpecialistOutput(domain="d", mode="chat", findings=findings,
                            evidence=evidence or [])


# ── number extraction ───────────────────────────────────────────────────────

def test_dates_are_not_read_as_quantities():
    """`2025-04` would otherwise contribute 2025 and 4, and every cited period
    would look like an unsupported number."""
    got = [w for w, _ in _numbers_in("TSR crossed in 2025-04 and 2025-05.")]
    assert got == []


@pytest.mark.parametrize("text,expect_value", [
    ("total was $3.93M", 3_930_000.0),
    ("8,888 transactions", 8888.0),
    ("spend rose 2.7x", 2.7),
    ("TSR 27.4", 27.4),
])
def test_written_forms_normalize(text, expect_value):
    vals = _numbers_in(text)[0][1]
    assert any(abs(v - expect_value) < 1e-6 for v in vals)


def test_percentages_allow_both_readings():
    """A claim of "41%" may be backed by 41 or 0.41 — accepting both keeps the
    check lenient, which is the right bias for an auditor."""
    vals = _numbers_in("utilization 41%")[0][1]
    assert 41.0 in vals and pytest.approx(0.41) in vals


# ── unsupported numbers ─────────────────────────────────────────────────────

def test_number_absent_from_every_tool_output_is_flagged():
    r = _Result(['{"rows_matching_filter": 8888, "total": 3927582.20}'])
    rep = audit_claims(r, _out("Spend totalled $9,999,999 across 8,888 txns."))
    assert any("9,999,999" in n for n in rep["unsupported_numbers"])


def test_number_present_in_the_output_is_not_flagged():
    r = _Result(['{"rows_matching_filter": 8888, "total": 3927582.2}'])
    rep = audit_claims(r, _out("8,888 transactions totalling 3,927,582.2."))
    assert rep["unsupported_numbers"] == []


def test_scaled_form_matches_an_unscaled_output_value():
    """"$3.93M" in prose vs 3927582.2 in the payload must not be a false
    positive — but 3.93M rounds, so this documents the tolerance boundary."""
    r = _Result(['{"total": 3930000}'])
    rep = audit_claims(r, _out("Spend totalled $3.93M."))
    assert rep["unsupported_numbers"] == []


def test_value_nested_in_a_batch_result_string_still_counts():
    """Batch payloads carry their inner result as a STRING; reading raw output
    text rather than parsed JSON is what makes those values visible."""
    inner = json.dumps({"series": [{"period": "2025-04", "value": 27.4}]})
    r = _Result([json.dumps({"results": [{"index": 0, "result": inner}]})])
    rep = audit_claims(r, _out("TSR peaked at 27.4."))
    assert rep["unsupported_numbers"] == []


def test_no_numbers_in_the_claim_is_silent():
    r = _Result(['{"rows_matching_filter": 8888}'])
    assert audit_claims(r, _out("No usable data for this domain."))[
        "unsupported_numbers"] == []


def test_evidence_bullets_are_audited_too():
    r = _Result(['{"total": 100}'])
    rep = audit_claims(r, _out("Total was 100.", evidence=["and 555 elsewhere"]))
    assert any("555" in n for n in rep["unsupported_numbers"])


# ── sample size reported as a count ─────────────────────────────────────────

def test_claiming_the_truncated_sample_size_is_flagged():
    """The known bug: counting `rows[]` instead of reading
    `rows_matching_filter`."""
    r = _Result(['{"rows_returned": 4, "rows_matching_filter": 8888}'])
    rep = audit_claims(r, _out("There were 4 matching transactions."))
    hit = rep["sample_size_as_count"][0]
    assert hit["rows_returned"] == 4 and hit["rows_matching_filter"] == 8888


def test_claiming_the_true_count_is_not_flagged():
    r = _Result(['{"rows_returned": 4, "rows_matching_filter": 8888}'])
    rep = audit_claims(r, _out("There were 8,888 matching transactions."))
    assert rep["sample_size_as_count"] == []


def test_no_flag_when_nothing_was_truncated():
    """returned == matching, so a claim of that number is correct."""
    r = _Result(['{"rows_returned": 12, "rows_matching_filter": 12}'])
    rep = audit_claims(r, _out("There were 12 matching transactions."))
    assert rep["sample_size_as_count"] == []


# ── the auditor must never break the turn ───────────────────────────────────

def test_unreadable_transcript_returns_an_empty_report():
    class _Broken:
        def to_input_list(self):
            raise RuntimeError("boom")

    assert audit_claims(_Broken(), _out("Spend was 123.")) == {
        "unsupported_numbers": [], "sample_size_as_count": []}


def test_missing_final_output_returns_an_empty_report():
    r = _Result(['{"total": 1}'])
    assert audit_claims(r, None)["unsupported_numbers"] == []


# ── provenance: what the answer was measured over ───────────────────────────
#
# The reviewer's own check is the strongest available — they know the domain.
# Forcing scope into the OUTPUT SCHEMA worked but cost tokens per bullet, so
# this derives it from the arguments the specialist already passed: free, and
# un-forgettable unlike a directive.

from agent_factories.agent_tools.claim_audit import measured_over


class _Calls:
    def __init__(self, calls):
        self.calls = calls

    def to_input_list(self):
        return [{"type": "function_call", "call_id": f"c{i}", "name": n,
                 "arguments": json.dumps(a)}
                for i, (n, a) in enumerate(self.calls)]


def test_provenance_names_table_column_op_and_filter():
    r = _Calls([("aggregate_column", {
        "table_name": "spends", "column": "Amount", "op": "share",
        "filter_column": "Merchant Name", "filter_op": "contains",
        "filter_value": "S BERTRAM"})])
    line = measured_over(r)[0]
    assert "spends.Amount" in line and "op=share" in line
    assert "Merchant Name contains 'S BERTRAM'" in line


def test_provenance_exposes_a_MISSING_time_filter():
    """The whole point: a share with no window, answering an "in 2025"
    question, must be visibly unscoped."""
    r = _Calls([("aggregate_column", {"table_name": "spends", "column": "Amount",
                                      "op": "share", "filter_column": "m",
                                      "filter_value": "A"})])
    line = measured_over(r)[0]
    assert "2025" not in line and "base=" not in line


def test_provenance_reports_an_explicit_base():
    r = _Calls([("aggregate_column", {
        "table_name": "spends", "column": "Amount", "op": "share",
        "filter_column": "m", "filter_value": "A",
        "base_filter_column": "Date", "base_filter_op": "contains",
        "base_filter_value": "2025"})])
    assert "base=Date contains '2025'" in measured_over(r)[0]


def test_provenance_skips_non_data_tools():
    r = _Calls([("kb_lookup", {"topic": "t"}),
                ("make_chart", {"topic": "t"}),
                ("get_chart_guidance", {})])
    assert measured_over(r) == []


def test_provenance_deduplicates_repeated_calls():
    call = ("summarize_trend", {"table_name": "model_scores",
                                "value_column": "x", "time_column": "trans_month"})
    assert len(measured_over(_Calls([call, call, call]))) == 1


def test_provenance_is_bounded():
    calls = [("query_table", {"table_name": f"t{i}", "column": "c"})
             for i in range(30)]
    assert len(measured_over(_Calls(calls))) <= 8


def test_provenance_survives_unparseable_arguments():
    r = _Calls([])
    r.to_input_list = lambda: [{"type": "function_call", "call_id": "c",
                                "name": "query_table", "arguments": "{oops"}]
    assert measured_over(r) == ["query_table(?)"]


def test_provenance_never_raises_on_a_broken_transcript():
    class _Broken:
        def to_input_list(self):
            raise RuntimeError("boom")
    assert measured_over(_Broken()) == []


# ── one-line scope, for the answer footnote ─────────────────────────────────

from agent_factories.agent_tools.claim_audit import scope_line


def test_scope_line_says_all_dates_when_unconstrained():
    """The load-bearing half: an unconstrained table answering a windowed
    question is the error this exposes, and silence would read as fine."""
    r = _Calls([("summarize_trend", {"table_name": "model_scores",
                                     "value_column": "x",
                                     "time_column": "trans_month"})])
    assert scope_line(r) == "model_scores: all dates"


def test_scope_line_reports_a_window_from_a_filters_list():
    r = _Calls([("query_table", {
        "table_name": "model_scores_transaction",
        "filters": '[{"column":"trans_dt","op":"between",'
                   '"value":"2025-05-01,2025-05-31"}]'})])
    assert scope_line(r) == "model_scores_transaction: 2025-05-01..2025-05-31"


def test_scope_line_covers_every_table_touched():
    r = _Calls([("aggregate_column", {"table_name": "spends", "column": "Amount"}),
                ("summarize_trend", {"table_name": "model_scores",
                                     "value_column": "x"})])
    out = scope_line(r)
    assert "spends: all dates" in out and "model_scores: all dates" in out


def test_scope_line_is_empty_without_data_calls():
    assert scope_line(_Calls([("kb_lookup", {"topic": "t"})])) == ""
