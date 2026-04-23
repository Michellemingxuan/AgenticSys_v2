"""Tests for FirewalledModel — the LangChain-backed firewall wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.firewall_stack import FirewallRejection, FirewallStack, FirewalledModel, FIREWALL_GUIDANCE
from logger.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-fwm", log_dir=str(tmp_path))


@pytest.fixture
def firewall(logger):
    return FirewallStack(logger=logger)


@pytest.fixture
def fake_model():
    """A LangChain-shaped chat model whose .ainvoke is mockable."""
    model = MagicMock()
    model.ainvoke = AsyncMock()
    model.bind_tools = MagicMock(return_value=model)  # bind_tools is sync, returns a model
    return model


def test_firewalled_model_construction(firewall, fake_model):
    fwm = firewall.wrap(fake_model)
    assert isinstance(fwm, FirewalledModel)
    assert fwm.firewall is firewall
    assert fwm.model is fake_model


from langchain_core.messages import AIMessage


@pytest.mark.asyncio
async def test_ainvoke_basic_text_response(firewall, fake_model):
    fake_model.ainvoke.return_value = AIMessage(content="42 is the answer")

    fwm = firewall.wrap(fake_model)
    result = await fwm.ainvoke(system_prompt="be helpful", user_message="what is 42?")

    assert result.status == "success"
    assert result.data == {"response": "42 is the answer"}
    assert len(firewall.step_history) == 1
    assert fake_model.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_ainvoke_retries_then_succeeds(firewall, fake_model):
    fake_model.ainvoke.side_effect = [
        FirewallRejection("PII", "contains PII"),
        AIMessage(content="safe answer"),
    ]

    fwm = firewall.wrap(fake_model)
    result = await fwm.ainvoke(system_prompt="be safe", user_message="hello 1234567")

    assert result.status == "success"
    assert result.data == {"response": "safe answer"}
    assert fake_model.ainvoke.call_count == 2

    # On retry, the system prompt should carry FIREWALL_GUIDANCE and the user
    # message should have its 6+-digit run masked.
    second_call_messages = fake_model.ainvoke.call_args_list[1][0][0]
    assert any("***MASKED***" in m.content for m in second_call_messages)
    assert any(FIREWALL_GUIDANCE in m.content for m in second_call_messages)


@pytest.mark.asyncio
async def test_ainvoke_returns_blocked_after_max_retries(firewall, fake_model):
    fake_model.ainvoke.side_effect = FirewallRejection("PII", "always blocked")

    fwm = firewall.wrap(fake_model)
    result = await fwm.ainvoke(system_prompt="x", user_message="y")

    assert result.status == "blocked"
    assert "always blocked" in result.error
    # default max_retries=2 → 1 initial + 2 retries = 3 attempts
    assert fake_model.ainvoke.call_count == 3


async def test_ainvoke_executes_tool_call(firewall, fake_model):
    """When the LLM emits tool_calls, FirewalledModel runs them and feeds results back."""
    from langchain_core.messages import AIMessage

    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    # Round 1: model asks to call `add(2, 3)`.
    tool_call_msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "add", "args": {"a": 2, "b": 3}, "id": "tc-1", "type": "tool_call"}
        ],
    )
    # Round 2: model emits final text after seeing the tool result.
    final_msg = AIMessage(content="The answer is 5")

    fake_model.ainvoke.side_effect = [tool_call_msg, final_msg]

    fwm = firewall.wrap(fake_model)
    result = await fwm.ainvoke(
        system_prompt="use tools",
        user_message="add 2 and 3",
        tools=[add],
    )

    assert result.status == "success"
    assert result.data == {"response": "The answer is 5"}
    fake_model.bind_tools.assert_called_once_with([add])
    assert fake_model.ainvoke.call_count == 2

    # Second invocation should include a ToolMessage carrying the tool result "5".
    second_msgs = fake_model.ainvoke.call_args_list[1][0][0]
    tool_msgs = [m for m in second_msgs if m.__class__.__name__ == "ToolMessage"]
    assert len(tool_msgs) == 1
    assert "5" in tool_msgs[0].content
