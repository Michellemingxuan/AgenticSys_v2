"""Tests for SafeChainAsyncOpenAI (the SafeChain shim). The actual safechain
package is unavailable in dev — these tests cover the message-translation and
response-synthesis logic, which are pure-Python and don't require safechain.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types

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


def test_try_parse_json_salvages_unclosed_object_with_trailing_markdown():
    # The real safechain failure mode (gpt-4.1, no constrained decoding): a
    # valid JSON object prefix, then the model spills into markdown and never
    # closes the object. Salvage the valid prefix.
    text = '{"answer": "x"\n\n- bullet one\n- bullet two'
    assert _try_parse_json(text) == {"answer": "x"}


def test_try_parse_json_salvages_trailing_data_after_closed_object():
    # Variant: object IS closed, then extra markdown follows ("Extra data").
    text = '{"answer": "y", "flags": []}\n\n- trailing commentary'
    assert _try_parse_json(text) == {"answer": "y", "flags": []}


def test_synthesize_output_salvages_orchestrator_markdown_spill():
    # Reproduces the prod ModelBehaviorError: the orchestrator emitted a valid
    # FinalAnswer object then spilled into markdown bullets without closing the
    # object, so the SDK's strict parse failed. The synthesized content must be
    # valid JSON the SDK can parse.
    text = (
        '{\n  "answer": "Spend total is $1.7M with a May 2025 peak."\n\n'
        "  - Aggregate spend: $1,720,483.53\n"
        "  - Monthly peak: May 2025 ($404,152)\n"
    )
    cc = _synthesize_chat_completion(text=text, model="gpt-4.1")
    msg = cc.choices[0].message
    assert msg.tool_calls is None
    parsed = json.loads(msg.content)  # must be valid JSON now (no raise)
    assert parsed["answer"].startswith("Spend total is $1.7M")


def test_synthesize_plain_prose_still_passes_through():
    # Regression guard: genuinely non-JSON prose must NOT be mangled by salvage.
    text = "Free-form answer with no JSON."
    cc = _synthesize_chat_completion(text=text, model="gpt-4.1")
    assert cc.choices[0].message.content == text


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

    async def fake_invoke(self, *, model, messages, tools, response_format, stream=False, **kw):
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

    async def fake_invoke(self, *, model, messages, tools, response_format, stream=False, **kw):
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


# ── model build via amodel + blocking chain.invoke in a worker thread ───────
#
# Prod API (per the private env): the model is built with `await amodel(id)`
# (async — token acquisition), cached and reused; the chain
# (`prompt | model | StrOutputParser`) is invoked SYNCHRONOUSLY in a dedicated
# worker thread, bounded by a per-call timeout. These tests pin that shape.


class _FakeModel:
    """Stand-in for the amodel-built model. `behavior(inputs)` is the result
    of the sync `chain.invoke` (or it raises)."""
    def __init__(self, behavior):
        self.behavior = behavior


class _FakeChain:
    def __init__(self, model):
        self._model = model

    def __or__(self, _parser):  # `... | StrOutputParser()` → same chain
        return self

    def invoke(self, inputs):
        return self._model.behavior(inputs)


class _FakePrompt:
    @classmethod
    def from_messages(cls, _msgs):
        return cls()

    def __or__(self, model):  # `prompt | model` → chain seeded with the model
        return _FakeChain(model)


class _FakeStrOutputParser:
    pass


def _install_fake_safechain(monkeypatch, *, amodel_factory):
    """Stub the safechain + langchain_core imports `_invoke` makes (neither is
    installed in dev). `amodel_factory()` returns the model for each
    `await amodel(...)` build."""
    prompts_mod = types.ModuleType("safechain.prompts")
    prompts_mod.ValidChatPromptTemplate = _FakePrompt
    core_model_mod = types.ModuleType("safechain.core.model")

    async def _amodel(_model_id):
        return amodel_factory()
    core_model_mod.amodel = _amodel

    lc_parsers = types.ModuleType("langchain_core.output_parsers")
    lc_parsers.StrOutputParser = _FakeStrOutputParser

    for name, mod in [
        ("safechain", types.ModuleType("safechain")),
        ("safechain.core", types.ModuleType("safechain.core")),
        ("safechain.core.model", core_model_mod),
        ("safechain.prompts", prompts_mod),
        ("langchain_core", types.ModuleType("langchain_core")),
        ("langchain_core.output_parsers", lc_parsers),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.mark.asyncio
async def test_invoke_builds_via_amodel_and_reuses_it(monkeypatch):
    builds = {"n": 0}

    def _factory():
        builds["n"] += 1
        return _FakeModel(lambda _in: '{"output": {"answer": "ok"}}')

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    result = await client.chat.completions.create(model="gpt-4o", messages=msgs)
    assert builds["n"] == 1  # amodel awaited once to build
    assert "ok" in result.choices[0].message.content

    # Cached: a second call reuses the model (no rebuild).
    await client.chat.completions.create(model="gpt-4o", messages=msgs)
    assert builds["n"] == 1


@pytest.mark.asyncio
async def test_invoke_per_call_timeout_raises(monkeypatch):
    """A hung (blocking) chain.invoke must surface as TimeoutError so the turn
    fails cleanly and releases the firewall semaphore + turn lock."""
    def _factory():
        def _slow(_in):
            time.sleep(0.5)  # blocking, in the worker thread
            return "late"
        return _FakeModel(_slow)

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    monkeypatch.setattr("llm.safechain_client._SAFECHAIN_CALL_TIMEOUT_S", 0.05)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    with pytest.raises(TimeoutError):
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )


@pytest.mark.asyncio
async def test_invoke_rebuilds_and_retries_on_401(monkeypatch):
    """A 401 (token expiry) rebuilds the model via amodel and retries once."""
    state = {"build": 0}

    def _factory():
        state["build"] += 1
        if state["build"] == 1:
            def _raise(_in):
                raise RuntimeError("HTTP 401 unauthorized")
            return _FakeModel(_raise)
        return _FakeModel(lambda _in: '{"output": {"answer": "after-refresh"}}')

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    result = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
    )
    assert state["build"] == 2  # initial build + rebuild on 401
    assert "after-refresh" in result.choices[0].message.content


@pytest.mark.asyncio
async def test_concurrent_calls_overlap_via_thread_pool(monkeypatch):
    """Concurrent calls run in the dedicated thread pool — they OVERLAP rather
    than serializing behind a single worker."""
    def _factory():
        def _slow(_in):
            time.sleep(0.2)
            return '{"output": {"answer": "ok"}}'
        return _FakeModel(_slow)

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=4)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    async def one_call():
        return await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.gather(one_call(), one_call(), one_call())
    elapsed = loop.time() - t0
    # 3 × 0.2s serialized would be ≥0.6s; overlapping in the pool is ≈0.2s.
    assert elapsed < 0.45, f"calls serialized ({elapsed:.2f}s) — pool not used"
