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
