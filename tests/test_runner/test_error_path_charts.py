"""A turn that dies at the orchestrator must not leave the Plots panel spinning.

`chart` was emitted only from `_finalize`, and the orchestrator-error branch
returns before that — so a turn whose specialists HAD plotted left
`chart_pending` placeholders that were never filled and never retracted.
"""
import types

import pytest

from runner.turn import conductor


class _Sess:
    def __init__(self, kb=None):
        self.events = []
        self.specialist_kb = kb or {}
        self.case_id = "CASE-1"
        self.logger = types.SimpleNamespace(log=lambda *a, **k: None)

    def emit(self, name, payload):
        self.events.append((name, payload))

    def names(self):
        return [n for n, _ in self.events]


def _runner(sess, pending):
    r = object.__new__(conductor.TurnRunner)
    r.sess = sess
    r.ctx = types.SimpleNamespace(_charts_pending=set(pending))
    return r


def test_orphaned_placeholders_are_retracted_when_no_chart_arrives():
    sess = _Sess()
    out = _runner(sess, {("bureau", "fico_trend")})._emit_charts_and_retract_pending(
        "t1", reason="turn_ended_before_charts_were_built")
    assert out == []
    assert sess.names() == ["chart_cancelled"]
    p = sess.events[0][1]
    assert (p["specialist"], p["topic"]) == ("bureau", "fico_trend")
    # The reason distinguishes "a duplicate was superseded" from "the turn died".
    assert p["reason"] == "turn_ended_before_charts_were_built"


def test_nothing_is_emitted_when_nothing_was_pending():
    sess = _Sess()
    assert _runner(sess, set())._emit_charts_and_retract_pending(
        "t1", reason="whatever") == []
    assert sess.events == []


def test_the_reason_is_carried_through(monkeypatch):
    """`_finalize` and the error path share this helper; only the reason differs,
    and it is the one thing telling a reader which case they are looking at."""
    sess = _Sess()
    _runner(sess, {("x", "y")})._emit_charts_and_retract_pending(
        "t1", reason="superseded_by_identical_chart")
    assert sess.events[0][1]["reason"] == "superseded_by_identical_chart"


def test_a_chart_that_exists_is_emitted_and_not_retracted(monkeypatch):
    sess = _Sess()
    monkeypatch.setattr(conductor, "_collect_turn_charts",
                        lambda kb, turn_id, case_id: [
                            {"specialist": "bureau", "topic": "fico_trend"}])
    monkeypatch.setattr(conductor, "_find_kp", lambda *a, **k: {"claim": "c"})
    monkeypatch.setattr(conductor, "_build_chart_payload",
                        lambda kp, c: {**c, "spec": {}})
    out = _runner(sess, {("bureau", "fico_trend")})._emit_charts_and_retract_pending(
        "t1", reason="turn_ended_before_charts_were_built")
    assert len(out) == 1
    assert sess.names() == ["chart"]          # emitted, so NOT cancelled
    assert sess.events[0][1]["turn_id"] == "t1"


def test_bookkeeping_failure_cannot_break_the_turn(monkeypatch):
    """This runs on an error path that has already emitted `final`; it must not
    raise a second terminal event."""
    sess = _Sess()
    r = _runner(sess, {("a", "b")})
    r.ctx = types.SimpleNamespace()           # no _charts_pending attribute
    assert r._emit_charts_and_retract_pending("t1", reason="x") == []
