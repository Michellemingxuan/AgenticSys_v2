"""An answer that DENIES what a tool returned.

Measured, case 11854808010. The specialist issued exactly the right call —
`query_table(payments, "Return Flag" eq "1")` — the tool returned
`rows_matching_filter: 1` with the row populated ($105,818.60 on 2025-04-28,
INSUFFICIENT FUNDS), and the answer said "No payment returns were found; there
are zero records in the payments table with Return Flag == 1". One row in, zero
reported, and the distiller wrote that into the KB as a high-confidence
knowledge point so every later turn inherited it as fact.

Nothing existing could see it: no tool failed, no filter matched zero, and the
claim carried no number to trace. The only evidence is that the two disagree.
"""
import json

from agent_factories.agent_tools.grounding import absence_contradicted_by_rows
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
        return self._items


def _out(findings, evidence=()):
    return SpecialistOutput(domain="spend_payments", mode="report",
                            findings=findings, evidence=list(evidence))


_ROW_RETURNED = json.dumps({
    "table": "payments_data", "rows_matching_filter": 1, "rows_returned": 1,
    "rows": [{"Payment Date": "2025-04-28", "Payment Amount": "105818.6",
              "Return Flag": "1"}]})
_ZERO_RETURNED = json.dumps({"table": "payments_data",
                             "rows_matching_filter": 0, "rows": []})
_REAL_CLAIM = ("No payment returns (returned payments) were found; there are "
               "zero records in the payments table with Return Flag == 1 for "
               "this customer.")


def test_catches_the_real_case_118_turn():
    hit = absence_contradicted_by_rows(
        _Result([_ROW_RETURNED]),
        _out(_REAL_CLAIM, ["payments table: 0 rows with Return Flag == 1"]))

    assert hit is not None
    assert hit["max_rows_matching"] == 1
    assert "No payment returns" in hit["claim"]


def test_a_zero_anywhere_is_licence_to_assert_absence():
    """A specialist that legitimately found nothing always has a zero to point
    at — requiring one is what keeps the false-positive rate near nil."""
    assert absence_contradicted_by_rows(
        _Result([_ROW_RETURNED, _ZERO_RETURNED]), _out(_REAL_CLAIM)) is None


def test_count_phrasing_counts_as_a_zero():
    """`aggregate_column` reports its count after the `=`, not as JSON."""
    assert absence_contradicted_by_rows(
        _Result(["count filtered by Return Flag eq '1' = 0 (out of 32 total rows)"]),
        _out(_REAL_CLAIM)) is None


def test_silent_when_no_absence_is_asserted():
    assert absence_contradicted_by_rows(
        _Result([_ROW_RETURNED]),
        _out("1 returned payment on 2025-04-28 for $105,818.60.")) is None


def test_silent_when_there_were_no_data_calls():
    """report_agent and chat-only runs have no counts to contradict."""
    assert absence_contradicted_by_rows(_Result([]), _out(_REAL_CLAIM)) is None


def test_never_raises_on_a_broken_transcript():
    class _Broken:
        def to_input_list(self):
            raise RuntimeError("boom")

    assert absence_contradicted_by_rows(_Broken(), _out(_REAL_CLAIM)) is None
    assert absence_contradicted_by_rows(_Result([_ROW_RETURNED]), None) is None


def test_phrasings_the_specialists_actually_use():
    for claim in ("No returned payments were found.",
                  "There were zero records matching.",
                  "None were found for this customer.",
                  "The customer did not have any payment returns.",
                  "No evidence of returned payments."):
        assert absence_contradicted_by_rows(
            _Result([_ROW_RETURNED]), _out(claim)) is not None, claim
