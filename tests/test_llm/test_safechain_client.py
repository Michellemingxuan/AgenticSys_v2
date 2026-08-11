"""Tests for SafeChainAsyncOpenAI (the SafeChain shim).

The shim now speaks NATIVE transport: the safechain model is a real LangChain
`AzureChatOpenAI` subclass, so tools / response_format / tool_choice are bound
with `.bind(**openai_kwargs)` and the reply is converted back from an
`AIMessage`. The old text protocol (tool schemas injected into the prompt, JSON
repaired out of prose) is gone — along with the ModelBehaviorError class of
failures it caused. See .claude/memory/safechain_dual_environment.md.

Neither `safechain` nor `langchain_core` is installed in dev, so the message
CONVERSION helpers are exercised directly (they are duck-typed and need no
imports), and the invoke path runs against stubbed modules. Final verification
of the real transport happens in the private env.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from unittest.mock import patch

from llm.factory import build_session_clients
from llm.firewall_stack import FIREWALL_GUIDANCE, FirewallRejection, FirewallStack
from llm.safechain_client import (
    SafeChainAsyncOpenAI,
    _SafeChainStream,
    _bind_kwargs,
    _chunk_from_message_chunk,
    _completion_from_message,
    _content_text,
    _inject_guidance,
    _prompt_excerpt,
    _to_lc_messages,
)
from logger.event_logger import EventLogger


# ── stand-ins for the LangChain message types ───────────────────────────────
#
# Duck-typed on purpose: the conversion helpers read attributes only, so these
# pin the CONTRACT (what the shim expects an AIMessage to look like) without
# needing langchain_core installed.

class _AIMessage:
    def __init__(self, content="", tool_calls=None, response_metadata=None, id=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.response_metadata = response_metadata or {}
        self.id = id


class _AIMessageChunk:
    def __init__(self, content="", tool_call_chunks=None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []


# ── AIMessage -> ChatCompletion ─────────────────────────────────────────────

def test_completion_from_plain_text_message():
    out = _completion_from_message(_AIMessage(content="hello"), "gpt-4.1")
    assert out.choices[0].message.content == "hello"
    assert out.choices[0].message.tool_calls is None
    assert out.choices[0].finish_reason == "stop"
    assert out.model == "gpt-4.1"


def test_completion_from_structured_output_keeps_raw_json_string():
    """With `response_format` bound, the model returns the JSON as CONTENT —
    the SDK parses it. The shim must not touch it."""
    body = '{"domain":"modeling","mode":"chat","findings":"TSR 39.6"}'
    out = _completion_from_message(_AIMessage(content=body), "gpt-4.1")
    assert out.choices[0].message.content == body


def test_completion_converts_tool_calls_and_json_encodes_args():
    """LangChain gives `args` as a dict; the OpenAI wire type needs a JSON
    STRING, which the SDK will json.loads back out."""
    msg = _AIMessage(tool_calls=[
        {"name": "get_weather", "args": {"city": "Phoenix"}, "id": "call_1",
         "type": "tool_call"},
    ])
    out = _completion_from_message(msg, "gpt-4.1")
    tc = out.choices[0].message.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.function.name == "get_weather"
    assert json.loads(tc.function.arguments) == {"city": "Phoenix"}
    assert out.choices[0].finish_reason == "tool_calls"


def test_completion_preserves_parallel_tool_calls():
    msg = _AIMessage(tool_calls=[
        {"name": "get_weather", "args": {"city": "Phoenix"}, "id": "c1"},
        {"name": "get_weather", "args": {"city": "Denver"}, "id": "c2"},
    ])
    out = _completion_from_message(msg, "gpt-4.1")
    assert [t.id for t in out.choices[0].message.tool_calls] == ["c1", "c2"]


def test_completion_synthesizes_id_for_a_tool_call_missing_one():
    out = _completion_from_message(
        _AIMessage(tool_calls=[{"name": "f", "args": {}}]), "gpt-4.1")
    assert out.choices[0].message.tool_calls[0].id


def test_completion_skips_nameless_tool_calls():
    """A malformed entry must not become a tool call with an empty name — the
    SDK would try to dispatch it and fail confusingly."""
    out = _completion_from_message(
        _AIMessage(content="x", tool_calls=[{"args": {}, "id": "c1"}]), "gpt-4.1")
    assert out.choices[0].message.tool_calls is None


def test_completion_normalizes_unknown_finish_reason():
    out = _completion_from_message(
        _AIMessage(content="x", response_metadata={"finish_reason": "weird"}),
        "gpt-4.1")
    assert out.choices[0].finish_reason == "stop"


def test_completion_keeps_length_finish_reason():
    """`length` is real and load-bearing — it means the reply was truncated."""
    out = _completion_from_message(
        _AIMessage(content="x", response_metadata={"finish_reason": "length"}),
        "gpt-4.1")
    assert out.choices[0].finish_reason == "length"


def test_content_text_joins_multimodal_parts():
    assert _content_text([{"type": "text", "text": "a"}, {"text": "b"}]) == "ab"
    assert _content_text("plain") == "plain"
    assert _content_text(None) == ""


# ── OpenAI wire messages -> LangChain ───────────────────────────────────────
#
# `_to_lc_messages` imports langchain_core lazily (absent in dev), so these
# install a stub whose classes carry the same names and constructor kwargs the
# real ones do — enough to pin the mapping.

@pytest.fixture
def lc_messages(monkeypatch):
    class _Base:
        def __init__(self, content="", **kw):
            self.content = content
            for k, v in kw.items():
                setattr(self, k, v)

    mod = types.ModuleType("langchain_core.messages")
    for name in ("AIMessage", "HumanMessage", "SystemMessage", "ToolMessage"):
        setattr(mod, name, type(name, (_Base,), {}))
    monkeypatch.setitem(sys.modules, "langchain_core",
                        types.ModuleType("langchain_core"))
    monkeypatch.setitem(sys.modules, "langchain_core.messages", mod)
    return mod


def test_to_lc_messages_maps_roles(lc_messages):
    lc = _to_lc_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ])
    assert [type(m).__name__ for m in lc] == [
        "SystemMessage", "HumanMessage", "AIMessage"]


def test_to_lc_messages_round_trips_a_tool_call_turn(lc_messages):
    """The assistant's tool_calls and the tool result must survive, bound by
    tool_call_id — otherwise the follow-up round references an unknown id and
    the provider rejects it."""
    lc = _to_lc_messages([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather",
                         "arguments": '{"city": "Phoenix"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "72F"},
    ])
    ai, tool = lc
    assert ai.tool_calls[0]["name"] == "get_weather"
    assert ai.tool_calls[0]["args"] == {"city": "Phoenix"}   # parsed, not a string
    assert tool.tool_call_id == "call_1"
    assert tool.content == "72F"


def test_to_lc_messages_survives_unparseable_tool_arguments(lc_messages):
    """Truncated arguments must not raise — degrade to {} and let the model
    correct itself on the next round."""
    lc = _to_lc_messages([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c", "function": {"name": "f", "arguments": '{"a": '}}]},
    ])
    assert lc[0].tool_calls[0]["args"] == {}


def test_to_lc_messages_stringifies_non_string_content(lc_messages):
    lc = _to_lc_messages([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert isinstance(lc[0].content, str)


# ── bind kwargs ─────────────────────────────────────────────────────────────

def test_bind_kwargs_passes_openai_shapes_through_untouched():
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    rf = {"type": "json_schema", "json_schema": {"name": "S", "schema": {}}}
    # `auto`: a round that MAY answer, so the schema rides along verbatim.
    out = _bind_kwargs(tools, "auto", rf)
    assert out["tools"] is tools           # verbatim — no translation
    assert out["response_format"] is rf
    assert out["tool_choice"] == "auto"


def test_bind_kwargs_drops_the_schema_on_a_forced_tool_call():
    """`required` means the model must return a tool call, so it cannot emit
    the structured answer — and on this transport the schema's mere presence
    routes the call through auto-parse. See llm/round_shaping.py."""
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    rf = {"type": "json_schema", "json_schema": {"name": "S", "schema": {}}}
    out = _bind_kwargs(tools, "required", rf)
    assert "response_format" not in out
    assert out["tools"] is tools           # everything else still verbatim
    assert out["tool_choice"] == "required"


def test_bind_kwargs_omits_absent_values():
    assert _bind_kwargs(None, None, None) == {}


def test_bind_kwargs_drops_tool_choice_without_tools():
    """tool_choice with no tools is a 400 from the provider; dropping it is
    better than failing the whole call."""
    assert "tool_choice" not in _bind_kwargs(None, "required", None)


def test_prompt_excerpt_is_telemetry_only():
    text = _prompt_excerpt([{"role": "system", "content": "s"},
                            {"role": "user", "content": "u"}])
    assert "system: s" in text and "user: u" in text


# ── streaming ───────────────────────────────────────────────────────────────

def test_chunk_conversion_forwards_partial_tool_args_verbatim():
    """tool_call_chunks carry PARTIAL argument strings; the SDK reassembles
    them. Parsing here would rebuild the repair layer we just deleted."""
    chunk = _chunk_from_message_chunk(
        _AIMessageChunk(tool_call_chunks=[
            {"name": "f", "args": '{"ci', "id": "c1", "index": 0}]),
        "gpt-4.1", "id1", 123)
    delta = chunk.choices[0].delta.tool_calls[0]
    assert delta.function.arguments == '{"ci'
    assert delta.index == 0


def test_chunk_conversion_skips_empty_chunks():
    assert _chunk_from_message_chunk(_AIMessageChunk(), "m", "i", 1) is None


async def _drain(stream):
    return [c async for c in stream]


@pytest.mark.asyncio
async def test_stream_brackets_body_with_role_and_finish():
    async def _gen():
        yield _AIMessageChunk(content="he")
        yield _AIMessageChunk(content="llo")

    chunks = await _drain(_SafeChainStream(agen=_gen(), model="gpt-4.1"))
    assert chunks[0].choices[0].delta.role == "assistant"
    assert "".join(c.choices[0].delta.content or "" for c in chunks) == "hello"
    assert chunks[-1].choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_finishes_with_tool_calls_when_any_were_seen():
    async def _gen():
        yield _AIMessageChunk(tool_call_chunks=[
            {"name": "f", "args": "{}", "id": "c1", "index": 0}])

    chunks = await _drain(_SafeChainStream(agen=_gen(), model="gpt-4.1"))
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_stream_close_is_safe():
    async def _gen():
        yield _AIMessageChunk(content="x")

    stream = _SafeChainStream(agen=_gen(), model="gpt-4.1")
    await stream.close()
    await stream.close()          # idempotent


# ── factory wiring ──────────────────────────────────────────────────────────

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


# ── retry-with-guidance ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_with_guidance_on_firewall_rejection():
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)
    seen_messages: list[list[dict]] = []

    async def fake_invoke(self, *, model, messages, tools, response_format,
                          stream=False, **kw):
        seen_messages.append([dict(m) for m in messages])
        if len(seen_messages) == 1:
            raise FirewallRejection("PII", "first attempt blocked")
        return _completion_from_message(_AIMessage(content="ok"), model)

    with patch("llm.safechain_client._SafeChainChatCompletions._invoke", new=fake_invoke):
        result = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Original system prompt."},
                      {"role": "user", "content": "Hi"}],
        )

    assert len(seen_messages) == 2
    assert FIREWALL_GUIDANCE in seen_messages[1][0]["content"]
    assert "ok" in result.choices[0].message.content


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)
    call_count = 0

    async def fake_invoke(self, *, model, messages, tools, response_format,
                          stream=False, **kw):
        nonlocal call_count
        call_count += 1
        raise FirewallRejection("PII", "always blocked")

    with patch("llm.safechain_client._SafeChainChatCompletions._invoke", new=fake_invoke):
        with pytest.raises(FirewallRejection):
            await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "system", "content": "s"},
                          {"role": "user", "content": "u"}])
    assert call_count == 3


def test_inject_guidance_redacts_and_appends():
    out = _inject_guidance([
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "CASE-12345 detail"},
    ])
    assert FIREWALL_GUIDANCE in out[0]["content"]
    assert "[CASE-ID]" in out[1]["content"]


@pytest.mark.asyncio
async def test_invoking_without_safechain_raises_clear_error():
    """In dev safechain isn't installed; fail fast and legibly."""
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)
    with pytest.raises(NotImplementedError, match="safechain"):
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"},
                      {"role": "user", "content": "u"}])


# ── model build + native chain, against stubbed safechain/langchain ─────────
#
# Pins the prod shape: model built with `await amodel(id)` (async — token
# acquisition), cached and reused; the chain is
# `ValidChatPromptTemplate | model.bind(**kwargs)` run via `ainvoke`, bounded by
# a timeout that actually aborts rather than orphaning work.
# See .claude/memory/safechain_async_and_thread_occupation.md.


class _FakeModel:
    """Stand-in for the amodel-built model. `behavior(inputs)` produces the
    result of `chain.ainvoke`: a value, a raise, or an awaitable."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.bound_kwargs: dict | None = None

    def bind(self, **kwargs):
        self.bound_kwargs = kwargs
        return self


class _FakeChain:
    def __init__(self, model):
        self._model = model

    async def ainvoke(self, inputs):
        result = self._model.behavior(inputs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    def astream(self, inputs):
        async def _gen():
            out = self._model.behavior(inputs)
            if asyncio.iscoroutine(out):
                out = await out
            yield _AIMessageChunk(content=getattr(out, "content", str(out)))
        return _gen()


class _FakePrompt:
    @classmethod
    def from_messages(cls, _msgs):
        return cls()

    def __or__(self, model):
        return _FakeChain(model)


def _install_fake_safechain(monkeypatch, *, amodel_factory):
    """Stub the safechain + langchain_core imports `_invoke` makes — neither is
    installed in dev."""
    prompts_mod = types.ModuleType("safechain.prompts")
    prompts_mod.ValidChatPromptTemplate = _FakePrompt
    core_model_mod = types.ModuleType("safechain.core.model")

    async def _amodel(_model_id):
        return amodel_factory()
    core_model_mod.amodel = _amodel

    lc_prompts = types.ModuleType("langchain_core.prompts")
    lc_prompts.MessagesPlaceholder = lambda name: name

    class _Msg:
        def __init__(self, content="", **kw):
            self.content = content
            for k, v in kw.items():
                setattr(self, k, v)

    lc_messages = types.ModuleType("langchain_core.messages")
    for _n in ("AIMessage", "HumanMessage", "SystemMessage", "ToolMessage"):
        setattr(lc_messages, _n, type(_n, (_Msg,), {}))

    for name, mod in [
        ("safechain", types.ModuleType("safechain")),
        ("safechain.core", types.ModuleType("safechain.core")),
        ("safechain.core.model", core_model_mod),
        ("safechain.prompts", prompts_mod),
        ("langchain_core", types.ModuleType("langchain_core")),
        ("langchain_core.prompts", lc_prompts),
        ("langchain_core.messages", lc_messages),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.mark.asyncio
async def test_invoke_builds_via_amodel_and_reuses_it(monkeypatch):
    builds = {"n": 0}

    def _factory():
        builds["n"] += 1
        return _FakeModel(lambda _in: _AIMessage(content="ok"))

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    result = await client.chat.completions.create(model="gpt-4o", messages=msgs)
    assert builds["n"] == 1
    assert result.choices[0].message.content == "ok"

    await client.chat.completions.create(model="gpt-4o", messages=msgs)
    assert builds["n"] == 1          # cached, no rebuild


@pytest.mark.asyncio
async def test_invoke_binds_tools_and_response_format_to_the_model(monkeypatch):
    """The SDK's own payload must reach `.bind()` unchanged — this is what
    replaces the whole text protocol."""
    captured = {}

    def _factory():
        model = _FakeModel(lambda _in: _AIMessage(content="ok"))
        captured["model"] = model
        return model

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    rf = {"type": "json_schema", "json_schema": {"name": "S", "schema": {}}}
    await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "u"}],
        tools=tools, response_format=rf, tool_choice="auto")

    bound = captured["model"].bound_kwargs
    assert bound["tools"] is tools
    assert bound["response_format"] is rf
    assert bound["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_invoke_per_call_timeout_raises(monkeypatch):
    def _factory():
        return _FakeModel(lambda _in: asyncio.sleep(0.5, result=_AIMessage("late")))

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    monkeypatch.setattr("llm.safechain_client._SAFECHAIN_CALL_TIMEOUT_S", 0.05)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    with pytest.raises(TimeoutError):
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"},
                      {"role": "user", "content": "u"}])


@pytest.mark.asyncio
async def test_invoke_rebuilds_and_retries_on_401(monkeypatch):
    state = {"build": 0}

    def _factory():
        state["build"] += 1
        if state["build"] == 1:
            def _raise(_in):
                raise RuntimeError("HTTP 401 unauthorized")
            return _FakeModel(_raise)
        return _FakeModel(lambda _in: _AIMessage(content="after-refresh"))

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    result = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "s"},
                  {"role": "user", "content": "u"}])
    assert state["build"] == 2
    assert result.choices[0].message.content == "after-refresh"


@pytest.mark.asyncio
async def test_403_and_400_become_firewall_rejections(monkeypatch):
    for code in ("403", "400"):
        def _factory(_c=code):
            def _raise(_in):
                raise RuntimeError(f"HTTP {_c} blocked")
            return _FakeModel(_raise)

        _install_fake_safechain(monkeypatch, amodel_factory=_factory)
        fw = FirewallStack(EventLogger(session_id="t"), max_retries=0,
                           concurrency_cap=2)
        client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)
        with pytest.raises(FirewallRejection):
            await client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "u"}])


@pytest.mark.asyncio
async def test_concurrent_calls_overlap(monkeypatch):
    """Native async `ainvoke` — concurrent calls overlap rather than serialize."""
    def _factory():
        return _FakeModel(lambda _in: asyncio.sleep(0.2, result=_AIMessage("ok")))

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=4)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    async def one_call():
        return await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"},
                      {"role": "user", "content": "u"}])

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.gather(one_call(), one_call(), one_call())
    elapsed = loop.time() - t0
    assert elapsed < 0.45, f"calls serialized ({elapsed:.2f}s) — not overlapping"


@pytest.mark.asyncio
async def test_invoke_cancellation_aborts_without_orphan(monkeypatch):
    """Regression guard for the pre-ainvoke "stuck after rewind" bug: cancelling
    must abort the underlying work, not leave it running to completion."""
    completed = {"n": 0}

    def _factory():
        async def _slow(_in):
            await asyncio.sleep(1.0)
            completed["n"] += 1
            return _AIMessage("late")
        return _FakeModel(lambda _in: _slow(_in))

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    task = asyncio.create_task(client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "s"},
                  {"role": "user", "content": "u"}]))
    await asyncio.sleep(0.1)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.time() - t0 < 0.2, "cancel did not return promptly"

    await asyncio.sleep(1.2)
    assert completed["n"] == 0, "work completed despite cancel — orphaned"


@pytest.mark.asyncio
async def test_stream_stall_between_chunks_times_out(monkeypatch):
    """A stalled transport must surface as TimeoutError. The non-streaming path
    gets this from `wait_for` around one await; a stream is many awaits, so it
    needs its own per-chunk bound or it would hang the turn indefinitely."""
    monkeypatch.setattr("llm.safechain_client._SAFECHAIN_CALL_TIMEOUT_S", 0.05)

    async def _stalls():
        yield _AIMessageChunk(content="first")
        await asyncio.sleep(5)
        yield _AIMessageChunk(content="never")

    stream = _SafeChainStream(agen=_stalls(), model="gpt-4.1")
    with pytest.raises(TimeoutError):
        await _drain(stream)


@pytest.mark.asyncio
async def test_stream_passes_through_a_long_but_healthy_stream(monkeypatch):
    """The bound is the GAP between chunks, not total duration — a long answer
    that keeps producing must not be cut off."""
    monkeypatch.setattr("llm.safechain_client._SAFECHAIN_CALL_TIMEOUT_S", 0.3)

    async def _slow_but_steady():
        for i in range(5):
            await asyncio.sleep(0.05)
            yield _AIMessageChunk(content=str(i))

    chunks = await _drain(_SafeChainStream(agen=_slow_but_steady(), model="gpt-4.1"))
    assert "".join(c.choices[0].delta.content or "" for c in chunks) == "01234"


# ── real token counts from usage_metadata ───────────────────────────────────

class _AIMessageWithUsage(_AIMessage):
    def __init__(self, usage_metadata=None, **kw):
        super().__init__(**kw)
        self.usage_metadata = usage_metadata


def test_usage_metadata_becomes_completion_usage():
    """The provider's own counts are exact; the tiktoken estimate could not see
    the tool schemas / response_format that were actually billed."""
    out = _completion_from_message(
        _AIMessageWithUsage(
            content="hi",
            usage_metadata={"input_tokens": 1234, "output_tokens": 56,
                            "total_tokens": 1290}),
        "gpt-4.1")
    assert out.usage.prompt_tokens == 1234
    assert out.usage.completion_tokens == 56
    assert out.usage.total_tokens == 1290


def test_usage_total_is_derived_when_absent():
    out = _completion_from_message(
        _AIMessageWithUsage(content="hi",
                            usage_metadata={"input_tokens": 10, "output_tokens": 4}),
        "gpt-4.1")
    assert out.usage.total_tokens == 14


def test_usage_is_none_when_the_build_reports_nothing():
    """Must stay None, not zero — the caller falls back to its estimate, and
    zeros would silently under-report cost."""
    assert _completion_from_message(_AIMessage(content="hi"), "gpt-4.1").usage is None
    assert _completion_from_message(
        _AIMessageWithUsage(content="hi", usage_metadata={}), "gpt-4.1").usage is None


@pytest.mark.asyncio
async def test_trace_prefers_real_counts_over_the_estimate(monkeypatch, tmp_path):
    """End-to-end through create(): the node trace must record the provider's
    numbers, including correcting the prompt estimate attached before the call."""
    from pathlib import Path
    from tools.node_trace import NodeTrace, NodeTraceStore
    import sqlite3

    store = NodeTraceStore(str(tmp_path / "t.db"))

    async def _stub_invoke(self_, *, model, messages, tools, response_format,
                           stream, **kw):
        return _completion_from_message(
            _AIMessageWithUsage(
                content="hi",
                usage_metadata={"input_tokens": 4321, "output_tokens": 77}),
            model)

    fw = FirewallStack(EventLogger(session_id="t", log_dir=str(tmp_path)))
    client = SafeChainAsyncOpenAI(model_name="gpt-4o-mini", firewall=fw)
    with patch("llm.safechain_client._SafeChainChatCompletions._invoke", _stub_invoke):
        async with NodeTrace(store, chat_id="c", case_id="c", turn_id="t",
                             node="specialist.modeling", depth=0):
            await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "short"}])

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    row = conn.execute(
        "SELECT prompt_tokens, completion_tokens FROM node_trace "
        "WHERE node LIKE '%round_1'").fetchone()
    assert row == (4321, 77)


# ── sampling / budget knobs reach the provider ──────────────────────────────
#
# Under the old TEXT transport `create()` did `del kw`, dropping every SDK
# extra — nothing could be forwarded, since the request was a prompt string.
# Native binding can honor them, and two are load-bearing: `max_tokens`
# (specialists raise it to 3000 because a 500-cap truncated a batch call
# mid-argument and the specialist fabricated numbers around the broken result)
# and `parallel_tool_calls` (the orchestrator opts in explicitly).

def test_bind_kwargs_forwards_the_allowlisted_knobs():
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    out = _bind_kwargs(tools, None, None,
                       extra={"max_tokens": 3000, "parallel_tool_calls": True,
                              "temperature": 0.2})
    assert out["max_tokens"] == 3000
    assert out["parallel_tool_calls"] is True
    assert out["temperature"] == 0.2


def test_bind_kwargs_drops_unset_knobs():
    out = _bind_kwargs(None, None, None,
                       extra={"max_tokens": None, "temperature": None})
    assert out == {}


def test_parallel_tool_calls_is_dropped_without_tools():
    """A 400 otherwise — and the orchestrator sets it on the final synthesis
    round too, where no tools remain."""
    out = _bind_kwargs(None, None, None, extra={"parallel_tool_calls": True})
    assert "parallel_tool_calls" not in out
    # max_tokens has no such restriction and must survive.
    assert _bind_kwargs(None, None, None,
                        extra={"max_tokens": 2000})["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_create_forwards_max_tokens_and_parallel_tool_calls(monkeypatch):
    """End-to-end through create(): the knobs the agent factories set must
    reach `.bind()`, not be swallowed by `del kw`."""
    captured = {}

    def _factory():
        m = _FakeModel(lambda _in: _AIMessage(content="ok"))
        captured["model"] = m
        return m

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "u"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        max_tokens=3000, parallel_tool_calls=True)

    bound = captured["model"].bound_kwargs
    assert bound["max_tokens"] == 3000
    assert bound["parallel_tool_calls"] is True


@pytest.mark.asyncio
async def test_create_still_ignores_unknown_sdk_extras(monkeypatch):
    """Allow-list, not passthrough — an unknown extra must not reach the
    provider and risk a 400."""
    captured = {}

    def _factory():
        m = _FakeModel(lambda _in: _AIMessage(content="ok"))
        captured["model"] = m
        return m

    _install_fake_safechain(monkeypatch, amodel_factory=_factory)
    fw = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o", firewall=fw)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "u"}],
        some_future_sdk_kwarg="boom", max_tokens=1234)

    bound = captured["model"].bound_kwargs or {}
    assert "some_future_sdk_kwarg" not in bound
    assert bound["max_tokens"] == 1234
