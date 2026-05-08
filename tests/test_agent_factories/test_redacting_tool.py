"""Tests for redacting_tool: verifies PII redaction at inter-agent transit boundaries."""
import json
import pytest
from unittest.mock import AsyncMock, patch

from agents import Agent

from agent_factories.redacting_tool import redacting_tool


@pytest.mark.asyncio
async def test_redacting_tool_sanitizes_input_to_inner_agent():
    """Input to inner agent must have PII stripped before Runner.run is called."""
    inner_agent = Agent(name="inner", instructions="x", tools=[])
    fake_result = type("R", (), {"final_output": "all clear"})()

    with patch(
        "agent_factories.redacting_tool.Runner.run",
        new=AsyncMock(return_value=fake_result),
    ) as mock_run:
        wrapped = redacting_tool(inner_agent, name="x", description="d")
        await wrapped.on_invoke_tool(
            None, json.dumps({"sub_question": "Investigate CASE-12345"})
        )

    assert mock_run.await_count == 1
    call_args = mock_run.call_args
    # Runner.run(agent, input, context=...)
    forwarded_input = (
        call_args.args[1]
        if len(call_args.args) > 1
        else call_args.kwargs.get("input")
    )
    assert "[CASE-ID]" in forwarded_input
    assert "12345" not in forwarded_input


@pytest.mark.asyncio
async def test_redacting_tool_redacts_output():
    """Output returned from inner agent must have PII stripped before reaching caller."""
    inner_agent = Agent(name="inner", instructions="x", tools=[])
    fake_result = type("R", (), {"final_output": "Found CASE-99999 issue"})()

    with patch(
        "agent_factories.redacting_tool.Runner.run",
        new=AsyncMock(return_value=fake_result),
    ):
        wrapped = redacting_tool(inner_agent, name="x", description="d")
        out = await wrapped.on_invoke_tool(
            None, json.dumps({"sub_question": "anything"})
        )

    assert "[CASE-ID]" in out
    assert "99999" not in out


@pytest.mark.asyncio
async def test_redacting_tool_name_and_description():
    """The returned FunctionTool must carry the supplied name and description."""
    inner_agent = Agent(name="inner", instructions="x", tools=[])
    wrapped = redacting_tool(inner_agent, name="specialist_tool", description="Ask specialist")

    assert wrapped.name == "specialist_tool"
    assert wrapped.description == "Ask specialist"


@pytest.mark.asyncio
async def test_redacting_tool_passes_inner_agent_to_runner():
    """Runner.run must be called with the correct inner agent as first positional arg."""
    inner_agent = Agent(name="inner", instructions="x", tools=[])
    fake_result = type("R", (), {"final_output": "ok"})()

    with patch(
        "agent_factories.redacting_tool.Runner.run",
        new=AsyncMock(return_value=fake_result),
    ) as mock_run:
        wrapped = redacting_tool(inner_agent, name="x", description="d")
        await wrapped.on_invoke_tool(None, json.dumps({"sub_question": "hello"}))

    call_args = mock_run.call_args
    forwarded_agent = call_args.args[0] if call_args.args else call_args.kwargs.get("agent")
    assert forwarded_agent is inner_agent


@pytest.mark.asyncio
async def test_redacting_tool_multi_turn_keeps_specialist_alive():
    """When the surrounding context exposes `_specialist_histories`, a second
    call to the wrapped tool must include the prior conversation history,
    so the specialist sub-agent stays "alive" across follow-up turns
    instead of starting fresh."""
    from types import SimpleNamespace
    from agents import RunContextWrapper

    inner_agent = Agent(name="inner", instructions="x", tools=[])

    # Each Runner.run call returns a fake RunResult whose to_input_list()
    # captures the input it received plus a synthetic assistant turn — so
    # the persisted history grows turn-over-turn.
    call_log = []

    def _make_result(received_input):
        captured = received_input
        class _Result:
            final_output = "ok"
            def to_input_list(self_):
                if isinstance(captured, list):
                    base = list(captured)
                else:
                    base = [{"role": "user", "content": captured}]
                return base + [{"role": "assistant", "content": "answer"}]
        return _Result()

    async def _fake_run(agent, run_input, context=None, **_kwargs):
        call_log.append(run_input)
        return _make_result(run_input)

    # Bare object that mimics AppContext's `_specialist_histories` field.
    app_ctx = SimpleNamespace(_specialist_histories={})
    wrapper = RunContextWrapper(app_ctx)

    with patch("agent_factories.redacting_tool.Runner.run", new=_fake_run):
        wrapped = redacting_tool(inner_agent, name="crossbu", description="d")

        # Turn 1: no prior history → input is the bare sub-question string.
        await wrapped.on_invoke_tool(
            wrapper, json.dumps({"sub_question": "how many consumer cards"})
        )
        assert isinstance(call_log[0], str)
        assert "consumer cards" in call_log[0]
        # History saved under the specialist's name.
        assert "crossbu" in app_ctx._specialist_histories
        assert len(app_ctx._specialist_histories["crossbu"]) == 2  # user + assistant

        # Turn 2: prior history must be prepended → input is now a list
        # carrying the previous user/assistant pair plus the new sub-q.
        await wrapped.on_invoke_tool(
            wrapper, json.dumps({"sub_question": "what about the commercial ones"})
        )
        assert isinstance(call_log[1], list)
        # Earlier turn's content survives.
        assert any(
            "consumer cards" in str(msg.get("content", ""))
            for msg in call_log[1]
        )
        # New sub-question appended at the end.
        assert call_log[1][-1] == {
            "role": "user",
            "content": "what about the commercial ones",
        }
        # History extended further.
        assert len(app_ctx._specialist_histories["crossbu"]) == 4


@pytest.mark.asyncio
async def test_redacting_tool_specialist_histories_isolated_per_specialist():
    """Two different specialist tools must NOT share history, even when
    invoked through the same AppContext. Each tool's name is the key."""
    from types import SimpleNamespace
    from agents import RunContextWrapper

    inner_a = Agent(name="a_inner", instructions="x", tools=[])
    inner_b = Agent(name="b_inner", instructions="x", tools=[])

    inputs_seen = []

    def _make_result(received_input):
        captured = received_input
        class _Result:
            final_output = "ok"
            def to_input_list(self_):
                base = (list(captured) if isinstance(captured, list)
                        else [{"role": "user", "content": captured}])
                return base + [{"role": "assistant", "content": "answer"}]
        return _Result()

    async def _fake_run(agent, run_input, context=None, **_kwargs):
        inputs_seen.append((agent.name, run_input))
        return _make_result(run_input)

    app_ctx = SimpleNamespace(_specialist_histories={})
    wrapper = RunContextWrapper(app_ctx)

    with patch("agent_factories.redacting_tool.Runner.run", new=_fake_run):
        a_tool = redacting_tool(inner_a, name="alpha", description="d")
        b_tool = redacting_tool(inner_b, name="beta", description="d")

        await a_tool.on_invoke_tool(wrapper, json.dumps({"sub_question": "for alpha"}))
        await b_tool.on_invoke_tool(wrapper, json.dumps({"sub_question": "for beta"}))

    # Each specialist gets a fresh first call (string input, not a list).
    assert isinstance(inputs_seen[0][1], str)
    assert isinstance(inputs_seen[1][1], str)
    # Histories are stored under independent keys.
    assert set(app_ctx._specialist_histories.keys()) == {"alpha", "beta"}
    assert "for alpha" in str(app_ctx._specialist_histories["alpha"])
    assert "for beta" in str(app_ctx._specialist_histories["beta"])
    # Beta's history doesn't carry alpha's prior content.
    assert "alpha" not in str(app_ctx._specialist_histories["beta"])


# ── Failure-path tests ──────────────────────────────────────────────────────
#
# These cover the wrapper's job of catching every exception class the inner
# Runner.run can raise (not just MaxTurnsExceeded), logging it, recording a
# structured entry on the AppContext, and returning a [FAILED ...] payload so
# the orchestrator LLM sees a clear failure signal instead of the SDK's
# generic "An error occurred while running the tool".


def _make_failure_ctx():
    """Tiny stand-in for AppContext that exposes the two attrs the wrapper
    writes to on failure: a logger and a `_specialist_errors` list."""
    from types import SimpleNamespace

    class _Logger:
        def __init__(self):
            self.events = []

        def log(self, evt, payload):
            self.events.append((evt, payload))

    return SimpleNamespace(
        logger=_Logger(),
        _specialist_histories={},
        _specialist_errors=[],
    )


@pytest.mark.asyncio
async def test_redacting_tool_records_max_turns_exceeded():
    """MaxTurnsExceeded must be caught, logged, and surface as a [FAILED ...]
    payload (not propagate up to function_tool's generic error handler)."""
    from agents import RunContextWrapper
    from agents.exceptions import MaxTurnsExceeded

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    ctx = _make_failure_ctx()

    async def _raise(*_a, **_kw):
        raise MaxTurnsExceeded("ran out of turns")

    with patch("agent_factories.redacting_tool.Runner.run", new=_raise):
        wrapped = redacting_tool(inner_agent, name="wcc", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "anything"})
        )

    assert out.startswith("[FAILED wcc]")
    assert "max_turns_exceeded" in out
    assert len(ctx._specialist_errors) == 1
    rec = ctx._specialist_errors[0]
    assert rec["specialist"] == "wcc"
    assert rec["error_type"] == "max_turns_exceeded"
    assert any(e[0] == "specialist_call_failed" for e in ctx.logger.events)


@pytest.mark.asyncio
async def test_redacting_tool_records_model_behavior_error():
    """ModelBehaviorError (malformed JSON / output-schema parse failure) must
    be caught with its real class name surfaced, not swallowed."""
    from agents import RunContextWrapper
    from agents.exceptions import ModelBehaviorError

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    ctx = _make_failure_ctx()

    async def _raise(*_a, **_kw):
        raise ModelBehaviorError("malformed JSON output")

    with patch("agent_factories.redacting_tool.Runner.run", new=_raise):
        wrapped = redacting_tool(inner_agent, name="domain_x", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "anything"})
        )

    assert "[FAILED domain_x]" in out
    assert "ModelBehaviorError" in out
    assert ctx._specialist_errors[0]["error_type"] == "ModelBehaviorError"


@pytest.mark.asyncio
async def test_redacting_tool_records_generic_exception():
    """Last-resort fence: a generic Exception (e.g., transport error) must
    still be captured rather than escape to the SDK's default handler."""
    from agents import RunContextWrapper

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    ctx = _make_failure_ctx()

    async def _raise(*_a, **_kw):
        raise RuntimeError("connection reset")

    with patch("agent_factories.redacting_tool.Runner.run", new=_raise):
        wrapped = redacting_tool(inner_agent, name="domain_y", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "anything"})
        )

    assert "[FAILED domain_y]" in out
    assert "RuntimeError" in out
    assert "connection reset" in out
    assert ctx._specialist_errors[0]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_redacting_tool_records_timeout():
    """asyncio.wait_for raising TimeoutError must surface as a structured
    failure rather than propagate up the stack."""
    import asyncio as _asyncio
    from agents import RunContextWrapper

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    ctx = _make_failure_ctx()

    async def _raise(*_a, **_kw):
        raise _asyncio.TimeoutError()

    # Patch wait_for itself so we don't have to actually wait the timeout.
    with patch("agent_factories.redacting_tool.asyncio.wait_for", new=_raise):
        wrapped = redacting_tool(inner_agent, name="domain_z", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "anything"})
        )

    assert "[FAILED domain_z]" in out
    assert "timeout" in out
    assert ctx._specialist_errors[0]["error_type"] == "timeout"


@pytest.mark.asyncio
async def test_redacting_tool_success_does_not_record_error():
    """The happy path must leave `_specialist_errors` empty."""
    from agents import RunContextWrapper

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    ctx = _make_failure_ctx()
    fake_result = type("R", (), {"final_output": "all good"})()

    with patch(
        "agent_factories.redacting_tool.Runner.run",
        new=AsyncMock(return_value=fake_result),
    ):
        wrapped = redacting_tool(inner_agent, name="ok_tool", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "hi"})
        )

    assert "[FAILED" not in out
    assert ctx._specialist_errors == []


# ── Knowledge-base + distiller tests ────────────────────────────────────────
#
# These cover the cross-turn KB plumbing wired in Phase 1 of the memory
# rework: redacting_tool reads a KB digest before each call, runs a distiller
# agent on the SpecialistOutput after, and persists new KnowledgePoints to a
# session-scoped dict that survives across turns.


from agent_factories.redacting_tool import (
    _active_kps,
    _format_kb_digest,
    _distill_and_persist,
)


def _make_kb_ctx(distiller=None, kb=None):
    """AppContext-shaped stand-in carrying the KB + distiller fields."""
    from types import SimpleNamespace

    class _Logger:
        def __init__(self):
            self.events = []

        def log(self, evt, payload):
            self.events.append((evt, payload))

    return SimpleNamespace(
        logger=_Logger(),
        _specialist_histories={},
        _specialist_errors=[],
        _specialist_kb=kb if kb is not None else {},
        _distiller=distiller,
        _turn_id="turn-test-1",
    )


def test_active_kps_keeps_latest_per_topic():
    """Older KPs with the same topic are retained in the list (audit) but
    `_active_kps` returns only the most recent per topic."""
    kps = [
        {"topic": "monthly_spend_trend", "claim": "v1", "captured_at_turn": "t1"},
        {"topic": "top_merchants", "claim": "m1", "captured_at_turn": "t1"},
        {"topic": "monthly_spend_trend", "claim": "v2-revised", "captured_at_turn": "t2"},
    ]
    active = _active_kps(kps)
    by_topic = {k["topic"]: k["claim"] for k in active}
    # Latest one wins per topic, older still present in the source list
    # (audit log) but not returned by the active filter.
    assert by_topic == {"monthly_spend_trend": "v2-revised", "top_merchants": "m1"}
    assert len(kps) == 3  # source list untouched


def test_format_kb_digest_empty_when_no_kps():
    assert _format_kb_digest([]) == ""
    assert _format_kb_digest(None) == ""


def test_format_kb_digest_renders_active_set_only():
    """The digest must reflect the active set (latest per topic), not the
    raw audit log. Confidence levels appear as bracketed tags."""
    kps = [
        {"topic": "fico_trajectory", "claim": "FICO 720→680 over 6 months",
         "confidence": "high", "source_call": "summarize_trend('bureau','fico_score',...)"},
        {"topic": "fico_trajectory", "claim": "FICO 720→645 (revised)",
         "confidence": "medium"},
    ]
    digest = _format_kb_digest(kps)
    assert "fico_trajectory" in digest
    assert "(revised)" in digest          # the active claim is the newer one
    assert "FICO 720→680" not in digest   # the older claim is hidden
    assert "[medium]" in digest


@pytest.mark.asyncio
async def test_redacting_tool_prepends_kb_digest_when_no_intra_turn_history():
    """First call within a turn (no `_specialist_histories[name]`) must see
    the cross-turn KB digest prepended to its sub-question."""
    from agents import RunContextWrapper

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    captured_inputs = []

    async def _fake_run(agent, run_input, context=None, **_kw):
        captured_inputs.append(run_input)
        return type("R", (), {"final_output": "ok",
                              "to_input_list": lambda self_: []})()

    ctx = _make_kb_ctx(
        distiller=None,  # no distiller wired → no second pass
        kb={"modeling": [
            {"topic": "delinquency_breaches",
             "claim": "times_30_dpd reached 3 in 2024-Q4 (risky > 1).",
             "confidence": "high"},
        ]},
    )

    with patch("agent_factories.redacting_tool.Runner.run", new=_fake_run):
        wrapped = redacting_tool(inner_agent, name="modeling", description="d")
        await wrapped.on_invoke_tool(
            RunContextWrapper(ctx),
            json.dumps({"sub_question": "show me the delinquency trajectory"}),
        )

    assert len(captured_inputs) == 1
    forwarded = captured_inputs[0]
    assert isinstance(forwarded, str)
    # The KB preface must be present along with the new question.
    assert "YOUR KNOWLEDGE BASE" in forwarded
    assert "delinquency_breaches" in forwarded
    assert "show me the delinquency trajectory" in forwarded
    # Section divider keeps the digest distinguishable from the question.
    assert "--- New question ---" in forwarded


@pytest.mark.asyncio
async def test_redacting_tool_skips_kb_digest_on_intra_turn_followup():
    """Second call within the same turn already has the digest in the
    `_specialist_histories` transcript; re-prepending would double it."""
    from agents import RunContextWrapper

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    captured_inputs = []

    async def _fake_run(agent, run_input, context=None, **_kw):
        captured_inputs.append(run_input)
        return type("R", (), {"final_output": "ok",
                              "to_input_list":
                              lambda self_: [{"role": "user", "content": "prior"},
                                             {"role": "assistant", "content": "ans"}]})()

    ctx = _make_kb_ctx(
        distiller=None,
        kb={"modeling": [{"topic": "x", "claim": "stale", "confidence": "high"}]},
    )
    # Simulate that this specialist was already called once this turn —
    # the prior transcript already contains the digest from that first call.
    ctx._specialist_histories["modeling"] = [
        {"role": "user", "content": "prior call w/ digest"},
        {"role": "assistant", "content": "prior answer"},
    ]

    with patch("agent_factories.redacting_tool.Runner.run", new=_fake_run):
        wrapped = redacting_tool(inner_agent, name="modeling", description="d")
        await wrapped.on_invoke_tool(
            RunContextWrapper(ctx),
            json.dumps({"sub_question": "follow-up question"}),
        )

    forwarded = captured_inputs[0]
    # Now the input is a list (prior transcript + new user message).
    assert isinstance(forwarded, list)
    new_user_msg = forwarded[-1]["content"]
    # The new user message must NOT carry the digest preface — that would
    # duplicate context already in the prior transcript.
    assert "YOUR KNOWLEDGE BASE" not in new_user_msg
    assert "follow-up question" in new_user_msg


@pytest.mark.asyncio
async def test_distiller_persists_knowledge_points_to_session_kb():
    """After a successful specialist run, the distiller's knowledge_points
    must land in `_specialist_kb[name]` keyed by specialist."""
    from agents import RunContextWrapper
    from models.types import KnowledgePoint, DistillerOutput

    # Stub distiller that returns two KPs.
    distiller = Agent(name="distiller", instructions="x", tools=[])
    new_kps = [
        KnowledgePoint(
            topic="monthly_spend_trend",
            claim="Spend rose $300 → $1100 over Nov-2024..Mar-2025.",
            numbers=[{"period": "2024-11", "value": 300},
                     {"period": "2025-03", "value": 1100}],
            viz={"kind": "trend", "x_field": "period", "y_field": "value"},
            confidence="high",
        ),
        KnowledgePoint(topic="top_merchant", claim="S BERTRAM 38% of spend.",
                       confidence="medium"),
    ]
    distiller_result = type("R", (), {
        "final_output": DistillerOutput(knowledge_points=new_kps),
    })()

    inner_agent = Agent(name="inner", instructions="x", tools=[])
    fake_specialist_result = type("R", (), {
        "final_output": "specialist findings here",
        "to_input_list": lambda self_: [],
    })()

    # Patch Runner.run to return the specialist result on the first call,
    # the distiller result on the second.
    call_count = {"n": 0}

    async def _fake_run(agent, run_input, context=None, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return fake_specialist_result
        return distiller_result

    ctx = _make_kb_ctx(distiller=distiller, kb={})

    with patch("agent_factories.redacting_tool.Runner.run", new=_fake_run):
        wrapped = redacting_tool(inner_agent, name="spend_payments", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx),
            json.dumps({"sub_question": "what's the spending pattern?"}),
        )

    # Specialist's payload still flows back to the orchestrator.
    assert "[FAILED" not in out
    # Both KPs persisted under the right specialist key.
    assert "spend_payments" in ctx._specialist_kb
    persisted = ctx._specialist_kb["spend_payments"]
    assert len(persisted) == 2
    topics = {kp["topic"] for kp in persisted}
    assert topics == {"monthly_spend_trend", "top_merchant"}
    # turn_id stamped onto the KP at distill time.
    assert all(kp.get("captured_at_turn") == "turn-test-1" for kp in persisted)
    # Distillation event logged.
    assert any(e[0] == "distiller_kps_added" for e in ctx.logger.events)


@pytest.mark.asyncio
async def test_distiller_failure_does_not_break_specialist_response():
    """When the distiller errors out, the specialist's payload still
    returns to the orchestrator and the KB simply doesn't grow this turn."""
    from agents import RunContextWrapper

    distiller = Agent(name="distiller", instructions="x", tools=[])
    inner_agent = Agent(name="inner", instructions="x", tools=[])
    specialist_result = type("R", (), {
        "final_output": "ok",
        "to_input_list": lambda self_: [],
    })()
    call_count = {"n": 0}

    async def _fake_run(agent, run_input, context=None, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return specialist_result
        # Distiller raises — must not affect the specialist's path.
        raise RuntimeError("distiller blew up")

    ctx = _make_kb_ctx(distiller=distiller, kb={})

    with patch("agent_factories.redacting_tool.Runner.run", new=_fake_run):
        wrapped = redacting_tool(inner_agent, name="bureau", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx),
            json.dumps({"sub_question": "any question"}),
        )

    # Specialist answer still flows.
    assert "[FAILED" not in out
    # KB didn't grow.
    assert ctx._specialist_kb == {}
    # Failure was logged.
    assert any(e[0] == "distiller_failed" for e in ctx.logger.events)


def test_distill_and_persist_noop_when_distiller_unwired():
    """Tests / legacy paths without _distiller or _specialist_kb must behave
    like the legacy single-turn flow — no errors, no KB updates."""
    import asyncio as _asyncio
    from types import SimpleNamespace

    ctx_no_distiller = SimpleNamespace(
        logger=None, _specialist_kb={}, _distiller=None, _turn_id=None,
    )
    n = _asyncio.get_event_loop().run_until_complete(
        _distill_and_persist(ctx_no_distiller, "x", "q", "out")
    )
    assert n == 0
    assert ctx_no_distiller._specialist_kb == {}
