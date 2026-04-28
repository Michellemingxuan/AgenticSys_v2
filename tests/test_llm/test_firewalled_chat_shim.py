"""Tests for FirewalledChatShim — preserves ChatAgent's ainvoke interface."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from openai import AsyncOpenAI

from llm.factory import FirewalledChatShim, build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import LLMResult


@pytest.mark.asyncio
async def test_shim_returns_llm_result_with_response():
    base = AsyncMock(spec=AsyncOpenAI)
    fake_choice = MagicMock()
    fake_choice.message.content = "Hello back."
    fake_resp = MagicMock(); fake_resp.choices = [fake_choice]
    base.chat.completions.create = AsyncMock(return_value=fake_resp)

    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=base)
    shim = FirewalledChatShim(clients)

    result = await shim.ainvoke(
        system_prompt="You are X.",
        user_message="Hi",
    )

    assert isinstance(result, LLMResult)
    assert result.status == "success"
    assert result.data == {"response": "Hello back."}


@pytest.mark.asyncio
async def test_shim_parses_output_type():
    """When output_type is a Pydantic model, parse the JSON string into a dict."""
    from pydantic import BaseModel

    class Foo(BaseModel):
        ok: bool
        msg: str

    base = AsyncMock(spec=AsyncOpenAI)
    fake_choice = MagicMock()
    fake_choice.message.content = json.dumps({"ok": True, "msg": "great"})
    fake_resp = MagicMock(); fake_resp.choices = [fake_choice]
    base.chat.completions.create = AsyncMock(return_value=fake_resp)

    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=base)
    shim = FirewalledChatShim(clients)

    result = await shim.ainvoke(
        system_prompt="x",
        user_message="y",
        output_type=Foo,
    )

    assert result.status == "success"
    assert result.data == {"ok": True, "msg": "great"}
