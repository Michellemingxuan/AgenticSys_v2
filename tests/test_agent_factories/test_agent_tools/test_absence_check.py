"""An answer that DENIES what a tool returned.

Measured, case 11854808010. The specialist issued exactly the right call —
`query_table(payments, "Return Flag" eq "1")` — the tool returned
`rows_matching_filter: 1` with the row populated ($105,818.60 on 2025-04-28,
INSUFFICIENT FUNDS), and the answer said "No payment returns were found; there
are zero records in the payments table with Return Flag == 1". One row in, zero
reported, and the distiller wrote that into the KP as a high-confidence
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


def test_scan_tool_errors_fails_open_on_a_broken_transcript():
    """`scan_tool_errors` is the one check with teeth — a hit quarantines the
    run from the KP — and its call site is unguarded, so an exception here
    would record the specialist as a HARD FAILURE and destroy the answer it was
    checking. "Could not run" must mean "no evidence of a problem"."""
    from agent_factories.agent_tools.grounding import scan_tool_errors

    class _Broken:
        def to_input_list(self):
            raise RuntimeError("malformed transcript")

    class _Hostile:
        def to_input_list(self):
            return [{"type": "function_call_output", "output": object()}]

    assert scan_tool_errors(_Broken()) == []
    assert scan_tool_errors(_Hostile()) == []
    assert scan_tool_errors(None) == []


# ── summarize_by_group: the enumeration IS the evidence ─────────────────────
#
# `summarize_by_group` lists only the values PRESENT, so it never emits a
# zero-count group and can never supply the zero the row rule asks for. It is
# still decisive, read differently: the group SET answers the question. Two real
# cases, both answered "zero returns", only one of them true:
#
#   366132845011   groups [{"0": 357}]              uniform  -> claim TRUE
#   11854808010    groups [{"0": 31}, {"1": 1}]     2 values -> claim FALSE

def _groupby(group_column, groups, n_total=None):
    return json.dumps({
        "table": "payments_data", "group_column": group_column,
        "value_column": "Payment Amount", "op": "count",
        "n_groups_total": n_total if n_total is not None else len(groups),
        "groups": [{"group": g, "value": str(n), "n_records": n} for g, n in groups],
    })


_ZERO_RETURNS = "Live data shows 100% successful payment settlement and zero returns."


def test_uniform_dimension_supports_the_absence_claim():
    """One group = every other category really is absent."""
    out = _groupby("Return Flag", [("0", 357)])
    assert absence_contradicted_by_rows(_Result([out]), _out(_ZERO_RETURNS)) is None


def test_a_second_value_in_the_dimension_contradicts_it():
    out = _groupby("Return Flag", [("0", 31), ("1", 1)])
    hit = absence_contradicted_by_rows(_Result([out]), _out(_ZERO_RETURNS))

    assert hit is not None
    assert hit["contradicted_by"] == "grouped dimension has >1 value present"


def test_an_unrelated_group_by_is_not_read_as_evidence():
    """A breakdown by merchant says nothing about returned payments. Firing on
    it would be the over-flagging this module exists to avoid."""
    out = _groupby("Merchant Name", [("AMAZON", 40), ("SHELL", 12), ("AT&T", 5)])
    assert absence_contradicted_by_rows(_Result([out]), _out(_ZERO_RETURNS)) is None


def test_the_enumeration_outranks_a_row_count_elsewhere():
    """A uniform enumeration of the claimed dimension settles it even when some
    other call in the run returned rows."""
    unrelated_rows = json.dumps({"table": "spends_data", "rows_matching_filter": 88})
    out = _groupby("Return Flag", [("0", 357)])
    assert absence_contradicted_by_rows(
        _Result([unrelated_rows, out]), _out(_ZERO_RETURNS)) is None


def test_group_by_singular_plural_tolerance():
    """The claim says "returns"; the column is "Return Flag"."""
    out = _groupby("Return Flag", [("0", 31), ("1", 1)])
    assert absence_contradicted_by_rows(
        _Result([out]), _out("There were no returns for this customer.")) is not None
