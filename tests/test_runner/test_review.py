import types
import pytest
from runner.turn import review as review_mod
from runner.turn.review import (
    _is_multi_specialist_turn, _dispatch_count, _bump_dispatch_count,
    _apply_review_directive, _review_trace_payload,
)


def test_multi_specialist_gate():
    ctx = types.SimpleNamespace(_domain_specialists_called={"a"})
    assert _is_multi_specialist_turn(ctx) is False
    ctx._domain_specialists_called = {"a", "b"}
    assert _is_multi_specialist_turn(ctx) is True


def test_dispatch_count_clamped_at_2():
    ctx = types.SimpleNamespace()
    assert _dispatch_count(ctx) == 0
    _bump_dispatch_count(ctx); _bump_dispatch_count(ctx); _bump_dispatch_count(ctx)
    assert _dispatch_count(ctx) == 2


# ── Reviewer visibility: general_specialist must surface in the trace ────────
#
# Regression for "general specialist is not invoked": the reviewer runs
# server-side and was invisible in the reasoning trace / orchestration-flow
# figure / node-trace. _apply_review_directive now emits agent_started +
# agent_completed + team_plan for `general_specialist` and appends a synthetic
# tool-call record (which the QA cache stores and the cache-hit path replays).


class _FakeLogger:
    def __init__(self):
        self.logs = []

    def log(self, ev, extra=None):
        self.logs.append((ev, extra))


class _FakeSess:
    def __init__(self):
        self.events = []
        self.logger = _FakeLogger()

    def emit(self, name, payload):
        self.events.append((name, payload))


def _fake_review(kind="coherent"):
    directive = types.SimpleNamespace(
        kind=kind, specialist=None, why=None, anchor=None)
    return types.SimpleNamespace(
        resolved=[1, 2], open_conflicts=[],
        cross_domain_insights=["spend spike aligns with driver decline"],
        directive=directive,
    )


def _events_named(sess, name):
    return [p for n, p in sess.events if n == name]


@pytest.mark.asyncio
async def test_apply_review_directive_emits_general_specialist_trace(monkeypatch):
    sess = _FakeSess()
    ctx = types.SimpleNamespace(
        _domain_specialists_called={"modeling", "spend_payments"},
        _dispatch_count=1,
    )
    tool_calls = [
        {"tool": "spend_payments", "payload": {"findings": "spend rose"}},
        {"tool": "modeling", "payload": {"findings": "drivers"}},
    ]

    async def fake_run_review(*a, **k):
        return _fake_review("coherent")

    monkeypatch.setattr(review_mod, "_run_review", fake_run_review)

    async def noop_redispatch(_inp):
        return None

    new_final, flags = await _apply_review_directive(
        sess=sess, ctx=ctx, framed_question="did spending spike? drivers?",
        tool_calls=tool_calls,
        streamed=types.SimpleNamespace(to_input_list=lambda: []),
        turn_id="t1", run_redispatch_pass_fn=noop_redispatch,
    )

    assert new_final is None  # coherent → no re-dispatch

    # Reasoning trace: started + completed for the reviewer.
    started = _events_named(sess, "agent_started")
    completed = _events_named(sess, "agent_completed")
    assert any(p["tool"] == "general_specialist" for p in started)
    gs_done = [p for p in completed if p["tool"] == "general_specialist"]
    assert len(gs_done) == 1
    assert gs_done[0]["payload"]["verdict"] == "coherent"
    assert gs_done[0]["payload"]["resolved"] == 2

    # Orchestration flow: team_plan re-emitted with the reviewer node, and the
    # synthetic tool-call is appended to tool_calls (what the QA cache stores).
    team_plan = _events_named(sess, "team_plan")[-1]
    assert any(c["tool"] == "general_specialist"
               for c in team_plan["tool_calls"])
    gs_records = [c for c in tool_calls if c["tool"] == "general_specialist"]
    assert len(gs_records) == 1
    assert gs_records[0]["payload"]["verdict"] == "coherent"
    assert "call_id" in gs_records[0] and "duration_ms" in gs_records[0]

    # The reviewer must NOT be fed its own output: specialist_outputs is built
    # before the append, so exactly the two domain specialists remain in front.
    assert [c["tool"] for c in tool_calls[:2]] == ["spend_payments", "modeling"]


@pytest.mark.asyncio
async def test_apply_review_directive_shows_reviewer_even_on_failure(monkeypatch):
    """On timeout/error _run_review returns None — the node still shows,
    marked review_failed, so the reviewer's attempt is visible."""
    sess = _FakeSess()
    ctx = types.SimpleNamespace(_domain_specialists_called={"a", "b"})
    tool_calls = [{"tool": "a", "payload": {}}, {"tool": "b", "payload": {}}]

    async def fake_run_review(*a, **k):
        return None

    monkeypatch.setattr(review_mod, "_run_review", fake_run_review)

    async def noop_redispatch(_inp):
        return None

    await _apply_review_directive(
        sess=sess, ctx=ctx, framed_question="q", tool_calls=tool_calls,
        streamed=types.SimpleNamespace(to_input_list=lambda: []),
        turn_id="t1", run_redispatch_pass_fn=noop_redispatch,
    )

    gs_done = [p for p in _events_named(sess, "agent_completed")
               if p["tool"] == "general_specialist"]
    assert len(gs_done) == 1
    assert gs_done[0]["payload"]["verdict"] == "review_failed"
    assert any(c["tool"] == "general_specialist" for c in tool_calls)


def test_review_trace_payload_none_is_review_failed():
    assert _review_trace_payload(None, None)["verdict"] == "review_failed"
