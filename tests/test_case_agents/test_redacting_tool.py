"""Tests for redacting_tool: verifies PII redaction at inter-agent transit boundaries."""
import json
import pytest
from unittest.mock import AsyncMock, patch

from agents import Agent

from case_agents.redacting_tool import redacting_tool


@pytest.mark.asyncio
async def test_redacting_tool_sanitizes_input_to_inner_agent():
    """Input to inner agent must have PII stripped before Runner.run is called."""
    inner_agent = Agent(name="inner", instructions="x", tools=[])
    fake_result = type("R", (), {"final_output": "all clear"})()

    with patch(
        "case_agents.redacting_tool.Runner.run",
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
        "case_agents.redacting_tool.Runner.run",
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
        "case_agents.redacting_tool.Runner.run",
        new=AsyncMock(return_value=fake_result),
    ) as mock_run:
        wrapped = redacting_tool(inner_agent, name="x", description="d")
        await wrapped.on_invoke_tool(None, json.dumps({"sub_question": "hello"}))

    call_args = mock_run.call_args
    forwarded_agent = call_args.args[0] if call_args.args else call_args.kwargs.get("agent")
    assert forwarded_agent is inner_agent
