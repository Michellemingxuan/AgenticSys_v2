"""Tests for SafeChainAsyncOpenAI (the SafeChain shim). The actual safechain
package is unavailable in dev — these tests cover the message-translation and
response-synthesis logic, which are pure-Python and don't require safechain.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, patch

from llm.factory import build_session_clients
from llm.firewall_stack import FIREWALL_GUIDANCE, FirewallRejection, FirewallStack
from llm.safechain_client import (
    SafeChainAsyncOpenAI,
    _build_tool_schema_block,
    _combine_messages,
    _inject_guidance,
    _synthesize_chat_completion,
    _try_parse_json,
)
from logger.event_logger import EventLogger


# ── pure helpers ─────────────────────────────────────────────────────────


def test_combine_messages_uses_neutral_role_labels():
    msgs = [
        {"role": "system", "content": "You are X."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "result body"},
    ]
    out = _combine_messages(msgs, tools=None, response_format=None)
    assert "Context:\nYou are X." in out
    assert "Request:\nhello" in out
    assert "Response:\nhi" in out
    assert "Tool result:\nresult body" in out
    # No raw bracketed roles like [SYSTEM]
    for marker in ("[SYSTEM]", "[USER]", "[ASSISTANT]"):
        assert marker not in out


def test_combine_messages_appends_tool_schema_to_first_system():
    tools = [{
        "type": "function",
        "function": {
            "name": "query_table",
            "description": "Query a table.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
    }]
    msgs = [{"role": "system", "content": "Sys"}, {"role": "user", "content": "Q"}]
    out = _combine_messages(msgs, tools=tools, response_format=None)
    # Tool schema block is in the system part
    sys_section = out.split("Request:")[0]
    assert "tool_call" in sys_section
    assert "query_table" in sys_section
    assert "Sys" in sys_section


def test_build_tool_schema_block_renders_each_tool():
    tools = [
        {"function": {"name": "a", "description": "alpha tool", "parameters": {}}},
        {"function": {"name": "b", "description": "beta tool", "parameters": {"x": "int"}}},
    ]
    text = _build_tool_schema_block(tools)
    assert '"tool_call"' in text
    assert '"output"' in text
    assert "- a" in text
    assert "- b" in text
    assert "alpha tool" in text


# ── response synthesis ───────────────────────────────────────────────────


def test_synthesize_tool_call():
    text = json.dumps({"tool_call": {"name": "creditrisk",
                                     "arguments": {"sub_question": "is it risky?"}}})
    cc = _synthesize_chat_completion(text=text, model="gpt-4o")
    msg = cc.choices[0].message
    assert msg.tool_calls is not None
    assert len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc.type == "function"
    assert tc.function.name == "creditrisk"
    assert json.loads(tc.function.arguments) == {"sub_question": "is it risky?"}
    assert msg.content is None
    assert cc.choices[0].finish_reason == "tool_calls"


def test_synthesize_output():
    text = json.dumps({"output": {"answer": "Low risk.", "flags": []}})
    cc = _synthesize_chat_completion(text=text, model="gpt-4o")
    msg = cc.choices[0].message
    assert msg.tool_calls is None
    parsed = json.loads(msg.content)
    assert parsed == {"answer": "Low risk.", "flags": []}


def test_synthesize_plain_text_passthrough():
    text = "Free-form answer with no JSON."
    cc = _synthesize_chat_completion(text=text, model="gpt-4o")
    msg = cc.choices[0].message
    assert msg.tool_calls is None
    assert msg.content == text


def test_try_parse_json_strips_markdown_fence():
    fenced = '```json\n{"output": {"x": 1}}\n```'
    parsed = _try_parse_json(fenced)
    assert parsed == {"output": {"x": 1}}


# ── factory dispatch ─────────────────────────────────────────────────────


def test_build_session_clients_defaults_to_openai():
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=1, concurrency_cap=2)
    from unittest.mock import MagicMock
    from openai import AsyncOpenAI as _AOAI
    clients = build_session_clients(fw, base_client=MagicMock(spec=_AOAI))
    assert clients.backend == "openai"


def test_build_session_clients_safechain_backend():
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=1, concurrency_cap=2)
    clients = build_session_clients(fw, model_name="gpt-4o", backend="safechain")
    assert clients.backend == "safechain"
    assert isinstance(clients.firewalled_client, SafeChainAsyncOpenAI)


def test_build_session_clients_picks_up_env_var(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "safechain")
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=1, concurrency_cap=2)
    clients = build_session_clients(fw)
    assert clients.backend == "safechain"


def test_build_session_clients_invalid_backend():
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=1, concurrency_cap=2)
    with pytest.raises(ValueError, match="Unknown LLM backend"):
        build_session_clients(fw, backend="anthropic")  # type: ignore[arg-type]


# ── retry-with-guidance + concurrency ────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_with_guidance_on_firewall_rejection():
    """First _invoke raises FirewallRejection, second succeeds. Verify
    FIREWALL_GUIDANCE was injected into the system message of the second call.
    """
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    # Capture each _invoke's messages so we can assert on the retry's content
    seen_messages: list[list[dict]] = []

    async def fake_invoke(self, *, model, messages, tools, response_format):
        seen_messages.append([dict(m) for m in messages])
        if len(seen_messages) == 1:
            raise FirewallRejection("PII", "first attempt blocked")
        return _synthesize_chat_completion(text='{"output": {"answer": "ok"}}', model=model)

    with patch("llm.safechain_client._SafeChainChatCompletions._invoke", new=fake_invoke):
        result = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Original system prompt."},
                {"role": "user", "content": "Hi"},
            ],
        )

    assert len(seen_messages) == 2  # first attempt + retry
    second_sys = seen_messages[1][0]
    assert FIREWALL_GUIDANCE in second_sys["content"]
    # And the result came from the retry
    assert "ok" in result.choices[0].message.content


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)
    call_count = 0

    async def fake_invoke(self, *, model, messages, tools, response_format):
        nonlocal call_count
        call_count += 1
        raise FirewallRejection("PII", "always blocked")

    with patch("llm.safechain_client._SafeChainChatCompletions._invoke", new=fake_invoke):
        with pytest.raises(FirewallRejection):
            await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            )
    # 1 original + 2 retries
    assert call_count == 3


def test_inject_guidance_redacts_and_appends():
    msgs = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "CASE-12345 detail"},
    ]
    out = _inject_guidance(msgs)
    assert FIREWALL_GUIDANCE in out[0]["content"]
    # User message has CASE-ID redacted
    assert "[CASE-ID]" in out[1]["content"]


# ── invocation without safechain installed → clear error ────────────────


@pytest.mark.asyncio
async def test_invoking_without_safechain_raises_clear_error():
    """In the dev env safechain isn't installed; using the shim should fail
    fast with a clear NotImplementedError, not a confusing ImportError."""
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)
    with pytest.raises(NotImplementedError, match="safechain"):
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )
