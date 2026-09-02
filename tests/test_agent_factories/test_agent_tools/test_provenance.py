"""Provenance: what was the answer MEASURED OVER?

`measured_over` gives the reasoning trace one line per data call; `scope_line`
collapses the whole run to `table: window` pairs for the reviewer-facing
answer. Both are derived from the arguments the specialist already passed, so
these tests pin the DERIVATION — a wrong scope line is worse than none, since
it tells a reviewer a number was measured over something it was not.

(The number-tracing `audit_claims` that used to share this module was removed:
claim verification is AgenticEval's job, and its `content/` pipeline does it
properly. See the module docstring.)
"""
import json

import pytest

from agent_factories.agent_tools.provenance import measured_over, scope_line
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


# ── unsupported numbers ─────────────────────────────────────────────────────


# ── sample size reported as a count ─────────────────────────────────────────


# ── the auditor must never break the turn ───────────────────────────────────


# ── provenance: what the answer was measured over ───────────────────────────
#
# The reviewer's own check is the strongest available — they know the domain.
# Forcing scope into the OUTPUT SCHEMA worked but cost tokens per bullet, so
# this derives it from the arguments the specialist already passed: free, and
# un-forgettable unlike a directive.

from agent_factories.agent_tools.provenance import measured_over


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
    r = _Calls([("kp_lookup", {"topic": "t"}),
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

from agent_factories.agent_tools.provenance import scope_line


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
    assert scope_line(_Calls([("kp_lookup", {"topic": "t"})])) == ""


def test_measured_over_keeps_the_threshold_in_a_multi_filter_call():
    """Regression: filters were dumped as raw JSON and hard-cut at 120 chars.

    The cut landed mid-token (`"op":"gte","valu`) and dropped the THRESHOLD —
    the one part a reviewer needs to tell whether a number was measured over
    the right set. Rendering structurally halves the length, so it fits.
    """
    r = _Calls([("transaction_detail", {
        "table_name": "model_scores_transaction",
        "filters": '[{"column":"trans_dt","op":"between",'
                   '"value":"2024-01-01,2025-06-30"},'
                   '{"column":"tot_struct_risk_score","op":"gte","value":"850"}]',
        "limit": 20})])
    line = measured_over(r)[0]
    assert "tot_struct_risk_score gte 850" in line       # threshold survives
    assert "trans_dt between 2024-01-01..2025-06-30" in line
    assert "limit=20" in line
    assert "…" not in line                                # nothing was cut
    assert '"column"' not in line                         # no raw JSON


def test_measured_over_marks_a_truncated_filter_list():
    """When a cap does bite it must be VISIBLE, not a line that ends mid-word."""
    filters = [
        {"column": f"column_with_a_long_name_{i}", "op": "eq", "value": f"value_{i}"}
        for i in range(12)
    ]
    line = measured_over(_Calls([("query_table", {
        "table_name": "spends", "filters": json.dumps(filters)})]))[0]
    assert "…" in line                       # the cut announces itself
    assert line.endswith("])")               # and the line still closes cleanly
    assert "column_with_a_long_name_0 eq value_0" in line  # earliest kept


def test_measured_over_survives_unparseable_filters():
    """Provenance must never break the turn."""
    line = measured_over(_Calls([("query_table", {
        "table_name": "spends", "filters": "{not json"})]))[0]
    assert "spends" in line


def test_scope_line_reads_tables_out_of_batch_specs():
    """The batch tools keep their tables in `specs_json`, not `table_name`.

    Regression: a specialist that used ONLY batch tools produced an EMPTY
    scope and scored 0 on provenance, even though `measured_over` had captured
    every table — the trace showed `batch_aggregate(?, specs=[…])` with the
    name sitting right there in the spec. Observed on payment_returns and
    tsr_cdss_trend across a 10-repeat run.
    """
    r = _Calls([("batch_aggregate", {"specs_json": json.dumps([
        {"table_name": "payments_data", "column": "Payment Date", "op": "count"},
    ])})])
    assert scope_line(r) == "payments_data: all dates"


def test_scope_line_covers_every_table_in_a_multi_spec_batch():
    r = _Calls([("batch_summarize_trend", {"specs_json": json.dumps([
        {"table_name": "modelling_data", "value_column": "credit_loss_prob"},
        {"table_name": "model_scores", "value_column": "cdss"},
    ])})])
    out = scope_line(r)
    assert "modelling_data: all dates" in out
    assert "model_scores: all dates" in out


def test_scope_line_takes_the_window_from_the_spec():
    r = _Calls([("batch_summarize_trend", {"specs_json": json.dumps([
        {"table_name": "model_scores",
         "filters": '[{"column":"trans_month","op":"between",'
                    '"value":"2024-09,2025-04"}]'},
    ])})])
    assert scope_line(r) == "model_scores: 2024-09..2025-04"


def test_scope_line_accepts_specs_already_parsed():
    """`specs_json` arrives as a list when the arguments were not re-encoded."""
    r = _Calls([("batch_aggregate", {"specs_json": [
        {"table_name": "spends", "column": "Amount", "op": "sum"},
    ]})])
    assert scope_line(r) == "spends: all dates"


def test_scope_line_survives_unparseable_specs():
    """Provenance must never break the turn — a bad spec yields no scope."""
    assert scope_line(_Calls([("batch_aggregate", {"specs_json": "{not json"})])) == ""


# ── the false positives measured on real runs ───────────────────────────────
#
# Three consecutive live runs flagged essentially nothing but noise:
#   modeling       -> ['2025', '$404,152']
#   spend_payments -> ['$392K', '$319K', '17%', '17%']
#   crossbu        -> ['67%', '33%', '67%', '33%']
# Bare years, correctly-rounded values, and computed percentages. An auditor
# whose output is all noise gets ignored, which is worse than not running it.


