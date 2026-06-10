"""SafeChainAsyncOpenAI — drop-in replacement for ``openai.AsyncOpenAI`` that
routes ``chat.completions.create()`` through SafeChain.

Same shape as :class:`llm.firewall_client.FirewalledAsyncOpenAI`, so the SDK's
``OpenAIChatCompletionsModel`` keeps working unchanged — only the underlying
HTTP transport differs.

**Where this is used.** Private/production environment, where direct OpenAI
access is blocked and all LLM traffic goes through the SafeChain pipeline.
In dev / this repo, ``backend="openai"`` keeps the existing
``FirewalledAsyncOpenAI`` path; ``backend="safechain"`` activates this shim.

**SafeChain's quirks** (mirrored from AgenticSys_v1/gateway/safechain_adapter.py):

- Accepts only a *single* human message — multi-turn lists must be flattened
  with neutral role labels (``Context``, ``Request``, ``Response``,
  ``Tool result``) so the SafeChain firewall doesn't pattern-match on
  ``[SYSTEM]`` / ``[USER]``.
- No native function-calling. Tool schemas are injected as text into the
  prompt; the LLM is instructed to emit ``{"tool_call": {...}}`` or
  ``{"output": {...}}`` JSON; this shim parses the JSON and synthesises an
  OpenAI ``ChatCompletion`` with a ``tool_calls=[…]`` array so the SDK
  thinks it received native tool-calls.
- HTTP 401 → token expiry → refresh the safechain model and retry once.
  HTTP 403 / 400 → raise :class:`FirewallRejection` — the existing retry-with-
  guidance loop in ``_FirewalledChatCompletions`` semantics is replicated here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import time
import uuid
from typing import Any

from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import (
    Choice as ChunkChoice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

from llm.firewall_stack import (
    FIREWALL_GUIDANCE,
    FirewallRejection,
    FirewallStack,
    sanitize_message,
)


# nest_asyncio patches asyncio.run/asyncio.get_event_loop so they tolerate
# being called from inside a running event loop. We need this because the
# safechain LLM's underlying client uses an async-only `TokenUtil.get_token`
# coroutine and bridges sync→async via `asyncio.run(...)` on its sync invoke
# path. Without nest_asyncio, that bridge raises
# "asyncio.run() cannot be called from a running event loop" the moment the
# server's per-turn event loop (or Jupyter's loop) is active.
#
# Module-level `apply()` is a no-op when patched twice, and is a noop import
# error when nest_asyncio isn't installed (dev env doesn't need it; private
# env should pip-install it as a small dependency).
_NEST_ASYNCIO_APPLIED = False
try:
    import nest_asyncio as _nest_asyncio  # type: ignore[import-not-found]

    _nest_asyncio.apply()
    _NEST_ASYNCIO_APPLIED = True
except ImportError:
    _nest_asyncio = None  # type: ignore[assignment]


# Per-call wall-clock cap on a single safechain LLM call. The safechain model
# is built async (`await amodel(...)`, which does the token acquisition), but
# the chain INVOKE is blocking/sync — so we run it in a worker thread and bound
# it with `asyncio.wait_for`. On timeout the asyncio side gives up promptly
# (freeing the firewall semaphore + turn lock); the underlying thread may
# linger until safechain returns, which is why we use a DEDICATED, generously
# sized pool below so a few stuck calls can't starve the shared default
# executor (the original "stuck at team construction" mechanism).
_SAFECHAIN_CALL_TIMEOUT_S = float(os.environ.get("SAFECHAIN_CALL_TIMEOUT_S", "180"))

# Dedicated thread pool for the blocking `chain.invoke()`. Sized well above the
# total LLM concurrency (specialist + orchestrator firewall caps) so a hung
# call holds a worker without queueing new calls behind dead threads.
_SAFECHAIN_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.environ.get("SAFECHAIN_THREAD_POOL", "32")),
    thread_name_prefix="safechain-invoke",
)


_ROLE_LABELS = {
    "system": "Context",
    "user": "Request",
    "assistant": "Response",
    "tool": "Tool result",
}


def _estimate_tokens(text: str, model: str) -> int:
    """tiktoken estimate with a robust fallback. Safechain doesn't return
    usage objects, so this is the only token signal we have on that path."""
    if not text:
        return 0
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


class SafeChainAsyncOpenAI:
    """Drop-in for ``openai.AsyncOpenAI`` that calls SafeChain underneath."""

    def __init__(self, *, model_name: str, firewall: FirewallStack):
        self._model_name = model_name
        self._firewall = firewall
        self._llm: Any = None  # lazy-initialised on first use
        self.chat = _SafeChainChat(self)

    # Endpoints the SDK / tracing layers may probe but don't route real
    # work through. Anything in this set raises AttributeError so callers
    # get a clear signal it's not supported. Everything else (passive
    # attrs like `base_url`, `api_key`, `timeout`, …) returns ``None`` —
    # the openai-agents SDK reads these for trace-export and telemetry,
    # and a None is enough for it to skip that path gracefully.
    _UNSUPPORTED_ENDPOINTS: frozenset = frozenset({
        "responses", "embeddings", "files", "images", "audio",
        "fine_tuning", "moderations", "completions", "batches",
        "uploads", "vector_stores", "assistants", "threads", "beta",
    })

    def __getattr__(self, name: str):
        # `chat` and other normal attributes are set on the instance and
        # never reach __getattr__.
        if name in type(self)._UNSUPPORTED_ENDPOINTS:
            raise AttributeError(
                f"SafeChainAsyncOpenAI does not expose '{name}'. Only chat "
                f"completions are routed through SafeChain."
            )
        # Dunder lookups (__class__, __reduce__, …) and underscore-prefixed
        # internals: be strict — raising lets Python fall through to the
        # type's MRO, which is the right behavior.
        if name.startswith("_"):
            raise AttributeError(
                f"SafeChainAsyncOpenAI has no internal attribute {name!r}."
            )
        # Benign passive attribute (base_url, api_key, timeout, max_retries,
        # default_headers, default_query, organization, project, …) — the
        # SDK reads these for tracing/logging only. Return None.
        return None

    async def _aensure_llm(self) -> Any:
        """Return the cached safechain model, building it once on first use.

        `amodel` is an ASYNC factory (it performs token acquisition), so it
        MUST be awaited — the model is then reused for every blocking
        `chain.invoke()`. A concurrent first-build race just builds twice and
        keeps the last; harmless (no lock, since the client is shared across
        per-turn event loops and an asyncio.Lock can't span loops)."""
        if self._llm is None:
            await self._arefresh_llm()
        return self._llm

    async def _arefresh_llm(self) -> None:
        """(Re)build the safechain model via `await amodel(...)`. First call and
        401 token-expiry retry. Caches on `self._llm`."""
        try:
            from safechain.core.model import amodel  # type: ignore[import-not-found]
        except ImportError as e:
            raise NotImplementedError(
                "safechain is not installed in this environment. "
                "SafeChainAsyncOpenAI is only usable in the private/prod env."
            ) from e
        model_id = os.environ.get("SAFECHAIN_MODEL", self._model_name)
        # Bound the build too. `amodel()` does token acquisition over the
        # network; if it hangs it would stall the whole turn with NO timeout
        # (the per-call timeout below only wraps the INVOKE), which shows up as
        # a round that's "stale, no input/output captured, never recovers".
        try:
            self._llm = await asyncio.wait_for(
                amodel(model_id), timeout=_SAFECHAIN_CALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"safechain amodel() build did not return within "
                f"{_SAFECHAIN_CALL_TIMEOUT_S:.0f}s"
            ) from e


class _SafeChainChat:
    def __init__(self, parent: SafeChainAsyncOpenAI):
        self.completions = _SafeChainChatCompletions(parent)


class _SafeChainChatCompletions:
    """Mimics ``AsyncOpenAI.chat.completions``. Only the ``create`` async
    method is needed — that's what the SDK calls."""

    def __init__(self, parent: SafeChainAsyncOpenAI):
        self._parent = parent

    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: Any = None,
        stream: bool = False,
        **kw: Any,
    ) -> Any:
        tool_choice = kw.pop("tool_choice", None)
        del kw  # absorbs remaining SDK extras we don't forward
        from tools.node_trace import (
            ACTIVE_NODE, NodeTrace, _hooks_own_rounds,
            attach_io, attach_latency, attach_usage,
        )
        from tools.node_trace.pricing import compute_cost
        firewall = self._parent._firewall
        attempt = 0
        # Pre-redact every outbound message (mirrors FirewalledAsyncOpenAI).
        messages = [_redact_message(m) for m in messages]
        while True:
            try:
                async with firewall.gate():
                    parent = ACTIVE_NODE.get()
                    if parent is None or parent.row_id <= 0:
                        return await self._invoke(
                            model=model, messages=messages, tools=tools,
                            response_format=response_format, stream=stream,
                            tool_choice=tool_choice,
                        )
                    if _hooks_own_rounds(parent):
                        return await self._invoke(
                            model=model, messages=messages, tools=tools,
                            response_format=response_format, stream=stream,
                            tool_choice=tool_choice,
                        )
                    round_idx = parent.next_round_index()
                    async with NodeTrace(
                        store=parent._store,
                        chat_id=parent.chat_id,
                        case_id=parent.case_id,
                        turn_id=parent.turn_id,
                        node=f"{parent.node}.round_{round_idx}",
                        depth=parent.depth + 1,
                    ):
                        combined = _combine_messages(messages, tools, response_format, tool_choice=tool_choice)
                        sys_chars = sum(
                            len(m.get("content") or "")
                            for m in messages if m.get("role") == "system"
                        )
                        p_tok = _estimate_tokens(combined, model)
                        attach_usage(
                            prompt_excerpt=combined,
                            prompt_tokens=p_tok,
                            system_prompt_chars=sys_chars or None,
                            model=model,
                        )
                        attach_io(messages_json=json.dumps(messages, default=str))
                        _llm_t0 = time.perf_counter()
                        resp = await self._invoke(
                            model=model, messages=messages, tools=tools,
                            response_format=response_format, stream=stream,
                            tool_choice=tool_choice,
                        )
                        attach_latency(
                            llm_call_ms=int((time.perf_counter() - _llm_t0) * 1000),
                        )
                        try:
                            if hasattr(resp, "choices") and resp.choices:
                                completion_text = resp.choices[0].message.content or ""
                            else:
                                completion_text = ""
                        except Exception:
                            completion_text = ""
                        c_tok = _estimate_tokens(completion_text, model)
                        attach_usage(
                            completion_excerpt=completion_text,
                            completion_tokens=c_tok,
                            cost_usd=compute_cost(
                                model=model,
                                prompt_tokens=p_tok,
                                completion_tokens=c_tok,
                            ),
                        )
                        # Store output for trace viewer (mirrors OpenAI path)
                        try:
                            out_json = resp.model_dump_json() if hasattr(resp, "model_dump_json") else None
                        except Exception:
                            out_json = None
                        if out_json is not None:
                            attach_io(output_json=out_json)
                        return resp
            except FirewallRejection as e:
                firewall.logger.log(
                    "firewall_rejection",
                    {"code": e.code, "message": e.message, "attempt": attempt,
                     "backend": "safechain"},
                )
                if attempt >= firewall.max_retries:
                    firewall.logger.log(
                        "firewall_blocked",
                        {"code": e.code, "message": e.message,
                         "attempts": attempt + 1, "backend": "safechain"},
                    )
                    raise
                attempt += 1
                messages = _inject_guidance(messages)

    async def _invoke(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        response_format: Any,
        stream: bool = False,
        tool_choice: str | None = None,
    ) -> Any:
        combined = _combine_messages(messages, tools, response_format, tool_choice=tool_choice)
        try:
            from safechain.prompts import ValidChatPromptTemplate  # type: ignore[import-not-found]
            from langchain_core.output_parsers import StrOutputParser  # type: ignore[import-not-found]
        except ImportError as e:
            raise NotImplementedError(
                "safechain is not installed — SafeChainAsyncOpenAI is for "
                "the private/prod environment only."
            ) from e

        # Build the model ASYNC (`await amodel(...)` does the token acquisition),
        # cached + reused. The chain INVOKE is blocking/sync, so run it in a
        # dedicated worker thread bounded by a per-call timeout: on timeout the
        # asyncio side gives up promptly (freeing the firewall semaphore + turn
        # lock) even though the worker may linger until safechain returns.
        llm = await self._parent._aensure_llm()

        def _sync_invoke(active_model: Any) -> str:
            chain = (
                ValidChatPromptTemplate.from_messages([("human", "{__input__}")])
                | active_model
                | StrOutputParser()
            )
            return chain.invoke({"__input__": combined})

        async def _run(active_model: Any) -> str:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(_SAFECHAIN_EXECUTOR, _sync_invoke, active_model),
                timeout=_SAFECHAIN_CALL_TIMEOUT_S,
            )

        try:
            text = await _run(llm)
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"safechain LLM call did not return within "
                f"{_SAFECHAIN_CALL_TIMEOUT_S:.0f}s"
            ) from e
        except Exception as e:  # noqa: BLE001 — we re-classify below
            es = str(e)
            if "401" in es:
                # Token expiry — rebuild the model and retry once.
                await self._parent._arefresh_llm()
                try:
                    text = await _run(await self._parent._aensure_llm())
                except asyncio.TimeoutError as te:
                    raise TimeoutError(
                        f"safechain LLM call did not return within "
                        f"{_SAFECHAIN_CALL_TIMEOUT_S:.0f}s (after token refresh)"
                    ) from te
            elif "403" in es:
                raise FirewallRejection("403", f"safechain blocked: {es}")
            elif "400" in es:
                raise FirewallRejection("400", f"safechain bad request: {es}")
            else:
                raise

        # The openai-agents SDK calls this with `stream=True` for streamed
        # runs (Runner.run_streamed). Return a synthetic single-chunk async
        # stream so the SDK's ChatCmplStreamHandler can iterate it the same
        # way it would a real OpenAI SSE stream — the underlying safechain
        # call is non-streaming but we already have the full text in hand.
        if stream:
            return _FakeAsyncStream(text=text, model=model, tools=tools)
        return _synthesize_chat_completion(text=text, model=model, tools=tools)


# ── helpers ──────────────────────────────────────────────────────────────


def _redact_message(message: dict) -> dict:
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": sanitize_message(content)}
    return message


def _inject_guidance(messages: list[dict]) -> list[dict]:
    """Append :data:`FIREWALL_GUIDANCE` to the first system message and
    re-redact every message. Mirrors the OpenAI-path retry loop."""
    out = []
    appended = False
    for m in messages:
        m = _redact_message(m)
        if not appended and m.get("role") == "system":
            m = {**m, "content": (m.get("content") or "") + "\n\n" + FIREWALL_GUIDANCE}
            appended = True
        out.append(m)
    return out


def _combine_messages(
    messages: list[dict],
    tools: list[dict] | None,
    response_format: Any,
    tool_choice: str | None = None,
) -> str:
    """Flatten a multi-turn message list into a single string with neutral
    role labels (``Context``, ``Request``, ``Response``, ``Tool result``) and
    append the tool-schema and response-format hints to the first system
    message (or prepended if none exists).

    BOTH hints are appended when both ``tools`` and ``response_format`` are
    set — that's the specialist's normal mode (data tools available AND a
    pydantic ``output_type``). Without the schema-bearing response_format
    hint, the LLM emits plain text instead of ``{"output": {…}}`` and the
    SDK's structured-output validator returns an empty ``final_output``.
    """
    parts: list[str] = []
    tool_block_appended = False
    rf_block_appended = False
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        if role == "system" and tools and not tool_block_appended:
            content = content + "\n\n" + _build_tool_schema_block(
                tools, tool_choice=tool_choice)
            tool_block_appended = True
        if (
            role == "system"
            and response_format is not None
            and not rf_block_appended
            and tool_choice != "required"
        ):
            content = content + "\n\n" + _build_response_format_hint(response_format)
            rf_block_appended = True
        label = _ROLE_LABELS.get(role, role.title() or "Context")
        parts.append(f"{label}:\n{content}")
    # No system message at all? Prepend the tool block + response_format hint
    # at the top of the prompt as a synthetic Context section.
    if (tools and not tool_block_appended) or (
        response_format is not None and not rf_block_appended
        and tool_choice != "required"
    ):
        synth: list[str] = []
        if tools and not tool_block_appended:
            synth.append(_build_tool_schema_block(
                tools, tool_choice=tool_choice))
        if (response_format is not None and not rf_block_appended
                and tool_choice != "required"):
            synth.append(_build_response_format_hint(response_format))
        parts.insert(0, "Context:\n" + "\n\n".join(synth))
    return "\n\n".join(parts)


def _build_tool_schema_block(tools: list[dict],
                             tool_choice: str | None = None) -> str:
    """Render the SDK's tool definitions as a text block the LLM can read.

    The SDK passes tools as OpenAI-style dicts with a ``function`` field that
    holds ``name``, ``description``, and ``parameters`` (JSON schema). We just
    render them in a stable text format and tell the LLM how to emit
    ``{"tool_call": …}`` / ``{"output": …}`` JSON.

    When ``tool_choice="required"``, injects mandatory language enforcing
    that the LLM must call at least one tool before emitting a final answer.
    """
    mandatory = tool_choice == "required"
    lines = [
        ("You MUST call tools now. Respond with ONLY a tool_call JSON."
         if mandatory else
         "You have access to the following tools."),
        "To call ONE tool, respond with ONLY this JSON (no other text, no markdown fences):",
        '  {"tool_call": {"name": "<tool_name>", "arguments": {<args>}}}',
        "",
        "To call MULTIPLE tools at once (parallel), respond with ONLY this JSON:",
        '  {"tool_calls": [',
        '    {"name": "<tool_a>", "arguments": {<args_a>}},',
        '    {"name": "<tool_b>", "arguments": {<args_b>}}',
        '  ]}',
        "",
        "Do NOT concatenate multiple {\"tool_call\": ...} objects in one response — "
        'use the {"tool_calls": [...]} array form instead.',
    ]
    if not mandatory:
        # Only show the output/final-answer format when the LLM is
        # allowed to finish (not on the first round where tool_choice
        # is required). This structurally prevents the LLM from
        # skipping tool calls — it doesn't know about {"output": ...}.
        lines += [
            "",
            "ANTI-REPETITION RULES (critical):",
            "  • Each tool should appear AT MOST ONCE per response. Never list the "
            "same tool name twice in a `tool_calls` array.",
            "  • Do NOT re-call a tool you have already called this turn with the "
            "same or trivially-rephrased sub-question — its prior result is in "
            "the conversation already.",
            "  • Once you have enough information from prior tool results to "
            "answer the question, IMMEDIATELY emit the `{\"output\": ...}` "
            "final answer below. Do not call more tools \"just in case\".",
            "",
            "When you have the final structured answer, respond with ONLY this JSON:",
            '  {"output": {<your structured answer matching output_schema>}}',
        ]
    lines += [
        "",
        "Available tools:",
    ]
    tool_names: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        name = fn.get("name", "?")
        tool_names.append(name)
        desc = (fn.get("description") or "").splitlines()[0] if fn.get("description") else ""
        params = fn.get("parameters") or {}
        try:
            params_repr = json.dumps(params, separators=(",", ":"))
        except (TypeError, ValueError):
            params_repr = str(params)
        lines.append(f"  - {name}")
        if desc:
            lines.append(f"      description: {desc}")
        lines.append(f"      parameters: {params_repr}")
    lines.append("")
    lines.append(
        "IMPORTANT: The ONLY valid tool names are: "
        + ", ".join(tool_names)
        + ". Filenames, column names, and other data values are ARGUMENTS "
        "to these tools, never tool names."
    )
    return "\n".join(lines)


def _build_response_format_hint(response_format: Any) -> str:
    """When the SDK requests a structured output but no tools, hint the LLM
    to emit ``{"output": {...}}`` matching the schema."""
    if isinstance(response_format, dict) and "json_schema" in response_format:
        schema = response_format["json_schema"].get("schema", {})
        try:
            schema_repr = json.dumps(schema, separators=(",", ":"))
        except (TypeError, ValueError):
            schema_repr = str(schema)
        return (
            'Respond with ONLY this JSON: {"output": {<answer matching schema>}}\n'
            f"Schema: {schema_repr}"
        )
    return 'Respond with ONLY this JSON: {"output": {<your structured answer>}}'


def _synthesize_chat_completion(
    *, text: str, model: str, tools: list[dict] | None = None,
) -> ChatCompletion:
    """Translate SafeChain's text response into an OpenAI ``ChatCompletion``.

    Three cases:
    - text parses as ``{"tool_call": {"name": ..., "arguments": ...}}`` →
      synthesise a ``ChatCompletion`` with ``tool_calls=[…]`` so the SDK
      sees a tool-call response.
    - text parses as ``{"output": {...}}`` → synthesise with the wrapped
      structured answer as ``content``.
    - otherwise → return text as ``content`` verbatim.
    """
    tool_calls, content, finish_reason = _extract_tool_calls_and_content(text)

    if tool_calls:
        tool_calls = _validate_and_repair_tool_calls(tool_calls, tools)
        if not tool_calls:
            content = text
            finish_reason = "stop"

    sdk_tool_calls: list[ChatCompletionMessageToolCall] | None = None
    if tool_calls:
        sdk_tool_calls = [
            ChatCompletionMessageToolCall(
                id=tc["id"],
                type="function",
                function=Function(name=tc["name"], arguments=tc["arguments"]),
            )
            for tc in tool_calls
        ]

    message = ChatCompletionMessage(
        role="assistant",
        content=content,
        tool_calls=sdk_tool_calls,
    )
    choice = Choice(index=0, message=message, finish_reason=finish_reason)
    return ChatCompletion(
        id=f"chatcmpl_{uuid.uuid4().hex[:24]}",
        choices=[choice],
        created=int(time.time()),
        model=model,
        object="chat.completion",
    )


def _extract_tool_calls_and_content(
    text: str,
) -> tuple[list[dict] | None, str | None, str]:
    """Parse safechain's text reply and decide whether it's a tool-call burst,
    a wrapped output, or plain content.

    Returns ``(tool_calls, content, finish_reason)`` where:

    - ``tool_calls`` — list of dicts ``{"id", "name", "arguments"}`` ready to
      be wrapped in either streaming or non-streaming SDK types. Empty list /
      None when no tool calls were emitted.
    - ``content`` — the assistant's text body when this is a content reply,
      otherwise ``None``.
    - ``finish_reason`` — ``"tool_calls"`` when at least one call was emitted,
      else ``"stop"``.

    Three shapes are accepted to absorb the LLM's natural variation:

    1. ``{"tool_call": {"name", "arguments"}}`` — single-call shape (preferred).
    2. ``{"tool_calls": [{"name", "arguments"}, ...]}`` — array shape, used
       when the orchestrator wants parallel tool execution.
    3. ``{"output": <answer>}`` — final structured answer, surfaced as
       ``content``.
    4. **Defensive**: if the body is multiple ``{"tool_call": ...}`` JSON
       objects concatenated (with newlines or whitespace between them — what
       the LLM sometimes does even after we ask for the array form), each
       gets parsed individually and merged into one ``tool_calls`` list.
    """
    parsed = _try_parse_json(text)

    # 1) Single tool_call.
    if isinstance(parsed, dict) and "tool_call" in parsed:
        tc = parsed["tool_call"]
        if isinstance(tc, dict):
            return _dedupe_tool_calls([_to_tool_call_dict(tc)]), None, "tool_calls"

    # 2) tool_calls array.
    if isinstance(parsed, dict) and "tool_calls" in parsed:
        arr = parsed["tool_calls"]
        if isinstance(arr, list) and arr:
            calls = [_to_tool_call_dict(tc) for tc in arr if isinstance(tc, dict)]
            if calls:
                return _dedupe_tool_calls(calls), None, "tool_calls"

    # 3) Wrapped output.
    if isinstance(parsed, dict) and "output" in parsed:
        out = parsed["output"]
        return None, (out if isinstance(out, str) else json.dumps(out)), "stop"

    # 4) Multiple concatenated tool_call objects (defensive).
    multi = _parse_concatenated_tool_calls(text)
    if multi:
        return _dedupe_tool_calls(multi), None, "tool_calls"

    # Plain content fallback. If the text parsed (possibly via the salvage in
    # `_try_parse_json`) as a JSON object that ISN'T a tool_call/output wrapper
    # — e.g. the orchestrator's FinalAnswer `{"answer": ...}` emitted directly
    # (its response-format hint is suppressed under tool_choice="required") —
    # hand the SDK the re-serialized clean JSON, NOT the raw text. The raw text
    # may carry trailing markdown the model spilled after the object, which
    # would then fail the SDK's strict Pydantic parse (ModelBehaviorError).
    if isinstance(parsed, dict):
        return None, json.dumps(parsed, ensure_ascii=False), "stop"

    # Genuine plain content (no parseable JSON) — pass through verbatim.
    return None, text, "stop"


def _dedupe_tool_calls(calls: list[dict]) -> list[dict]:
    """Drop tool calls that duplicate an earlier one in the same response.

    The orchestrator (especially without native function-calling) sometimes
    emits the same tool call twice in one ``tool_calls`` array — same
    specialist, same sub-question. The SDK then fans out parallel duplicates
    against the same specialist, doubling cost and burning the turn budget
    while the redacting-tool's per-AppContext dedup races (parallel calls
    both miss an empty cache before either has finished).

    Cutting duplicates here, BEFORE the SDK ever sees them, is the only
    point in the pipeline where parallel duplicates can be eliminated
    deterministically. Match key is ``(tool_name, normalized_arguments)``
    so trivially-rephrased sub-questions ("Did the customer have any
    payment returns?" vs "did the customer have any payment returns? ")
    map to the same call. Each surviving call keeps its original ``id``
    so the SDK's tool-result correlation isn't disturbed.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for tc in calls:
        # Normalize the arguments JSON: parse + re-serialize sorted, lowercase
        # the human-text fields. We only normalize string values (sub_question
        # is the typical one); numeric / structured args round-trip unchanged.
        try:
            parsed_args = json.loads(tc.get("arguments", "{}") or "{}")
        except (json.JSONDecodeError, ValueError):
            parsed_args = {"_raw": str(tc.get("arguments", ""))}
        if isinstance(parsed_args, dict):
            norm = {
                k: " ".join(str(v).strip().lower().split()) if isinstance(v, str) else v
                for k, v in parsed_args.items()
            }
        else:
            norm = parsed_args
        key = (tc.get("name", ""), json.dumps(norm, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(tc)
    return out


_log = logging.getLogger(__name__)


def _validate_and_repair_tool_calls(
    tool_calls: list[dict] | None,
    tools: list[dict] | None,
) -> list[dict] | None:
    """Validate tool-call names against the known tool set and auto-correct.

    SafeChain's text-based tool schema sometimes confuses the model into using
    argument values (e.g. filenames like ``modeling_exp_0.md``) as tool names.
    When a name doesn't match any known tool, we try to find the correct one
    by matching the call's argument keys against each tool's parameter schema.
    """
    if not tool_calls or not tools:
        return tool_calls

    known_params: dict[str, set[str]] = {}
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        tname = fn.get("name", "")
        props = (fn.get("parameters") or {}).get("properties") or {}
        known_params[tname] = set(props.keys())

    known_names = set(known_params.keys())
    repaired: list[dict] = []

    for tc in tool_calls:
        if tc["name"] in known_names:
            repaired.append(tc)
            continue

        try:
            arg_keys = set(json.loads(tc.get("arguments", "{}") or "{}").keys())
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue

        best_match = None
        best_overlap = 0
        for tname, tparams in known_params.items():
            overlap = len(arg_keys & tparams)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = tname

        if best_match and best_overlap > 0:
            _log.warning(
                "safechain tool-name repair: %r → %r (matched on arg keys %s)",
                tc["name"], best_match, arg_keys & known_params[best_match],
            )
            repaired.append({**tc, "name": best_match})

    return repaired if repaired else None


def _to_tool_call_dict(tc: dict) -> dict:
    """Normalise one tool-call dict into the shape both synth functions expect."""
    args = tc.get("arguments", tc.get("args", {}))
    args_json = args if isinstance(args, str) else json.dumps(args or {})
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "name": str(tc.get("name", "")),
        "arguments": args_json,
    }


def _parse_concatenated_tool_calls(text: str) -> list[dict]:
    """Try to recover when the LLM emits several ``{"tool_call": ...}`` JSON
    objects back-to-back instead of using the ``{"tool_calls": [...]}`` array
    form. We greedily decode JSON values from a stream and collect every
    ``{"tool_call": {...}}`` we find. Returns [] if the text isn't a stream
    of well-formed JSON values, or if none of the values were tool_call shapes.
    """
    if not isinstance(text, str):
        return []
    # Strip optional markdown fences first.
    s = text.strip()
    for fence in ("```json", "```"):
        if s.startswith(fence):
            s = s[len(fence):].lstrip("\n")
            break
    if s.endswith("```"):
        s = s[:-3].rstrip()

    decoder = json.JSONDecoder()
    out: list[dict] = []
    i = 0
    n = len(s)
    while i < n:
        # Skip leading whitespace + optional list/array commas the LLM might
        # have introduced.
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        try:
            obj, end = decoder.raw_decode(s, i)
        except (json.JSONDecodeError, ValueError):
            return []  # not a valid stream of JSON values — bail out
        if isinstance(obj, dict) and "tool_call" in obj:
            tc = obj["tool_call"]
            if isinstance(tc, dict):
                out.append(_to_tool_call_dict(tc))
        elif isinstance(obj, dict) and "tool_calls" in obj:
            arr = obj["tool_calls"]
            if isinstance(arr, list):
                for tc in arr:
                    if isinstance(tc, dict):
                        out.append(_to_tool_call_dict(tc))
        # Anything else in the stream we silently skip — the caller falls
        # back to plain-content if `out` ends up empty.
        i = end
    return out


def _synthesize_chat_chunks(
    *, text: str, model: str, tools: list[dict] | None = None,
) -> list[ChatCompletionChunk]:
    """Translate SafeChain's full text response into ChatCompletionChunks.

    The openai-agents SDK's ``ChatCmplStreamHandler.handle_stream`` iterates
    a stream of chunks and accumulates state from ``chunk.choices[0].delta``.
    SafeChain returns the full body in one shot (no SSE), so we emit a small
    fixed sequence of chunks that, taken together, look like the equivalent
    of a real OpenAI stream:

    1. role chunk           — establishes the assistant role
    2. content / tool_calls — the actual payload
    3. finish chunk         — finish_reason terminator
    """
    tool_calls, content, finish_reason = _extract_tool_calls_and_content(text)

    if tool_calls:
        tool_calls = _validate_and_repair_tool_calls(tool_calls, tools)
        if not tool_calls:
            content = text
            finish_reason = "stop"

    chunk_id = f"chatcmpl_{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def _make_chunk(delta: ChoiceDelta, finish_reason: str | None = None) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id=chunk_id,
            choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish_reason)],
            created=created,
            model=model,
            object="chat.completion.chunk",
        )

    chunks: list[ChatCompletionChunk] = [_make_chunk(ChoiceDelta(role="assistant"))]

    if tool_calls:
        # One delta per tool call, each at its own `index`. The SDK's
        # ChatCmplStreamHandler accumulates by index, so multiple parallel
        # tool calls end up as a list on the final response.
        for i, tc in enumerate(tool_calls):
            chunks.append(_make_chunk(ChoiceDelta(
                tool_calls=[ChoiceDeltaToolCall(
                    index=i,
                    id=tc["id"],
                    type="function",
                    function=ChoiceDeltaToolCallFunction(
                        name=tc["name"],
                        arguments=tc["arguments"],
                    ),
                )],
            )))
        chunks.append(_make_chunk(ChoiceDelta(), finish_reason="tool_calls"))
        return chunks

    chunks.append(_make_chunk(ChoiceDelta(content=content or "")))
    chunks.append(_make_chunk(ChoiceDelta(), finish_reason=finish_reason))
    return chunks


class _FakeAsyncStream:
    """Async-iterable wrapper around a pre-built list of ChatCompletionChunks.

    The openai-agents SDK uses ``isinstance(ret, ChatCompletion)`` to decide
    whether the LLM call is non-streaming (return as-is) or streaming (wrap
    in a Response + iterate as an SSE-style stream). For ``stream=True``,
    we hand back this object — anything that is *not* a ChatCompletion and
    is async-iterable is treated as a stream by the SDK's handler.
    """

    def __init__(self, *, text: str, model: str, tools: list[dict] | None = None) -> None:
        self._chunks = _synthesize_chat_chunks(text=text, model=model, tools=tools)
        self._idx = 0

    def __aiter__(self) -> "_FakeAsyncStream":
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk

    async def close(self) -> None:
        # Real openai AsyncStream exposes close(); the SDK may call it.
        self._idx = len(self._chunks)


def _repair_truncated_json(text: str) -> str | None:
    """Attempt to repair JSON truncated mid-way by SafeChain's output buffer.

    Strategy: close any open strings, then append enough closing braces/brackets
    to balance the nesting. This recovers partial SpecialistOutput JSON like:
      {"output": {"domain": "modeling", "findings": "TSR rose...
    into valid JSON with truncated field values.
    """
    s = text.rstrip()
    if not s:
        return None

    # Close any open string
    quote_count = 0
    in_escape = False
    for ch in s:
        if in_escape:
            in_escape = False
            continue
        if ch == "\\":
            in_escape = True
            continue
        if ch == '"':
            quote_count += 1
    if quote_count % 2 == 1:
        s += '"'

    # Close open arrays and objects
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()

    s += "".join(reversed(stack))
    return s


def _try_parse_json(text: str) -> Any:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Strip markdown fences.
    stripped = text.strip()
    for fence in ("```json", "```"):
        if stripped.startswith(fence):
            stripped = stripped[len(fence):].lstrip("\n")
            break
    if stripped.endswith("```"):
        stripped = stripped[:-3].rstrip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    # Attempt truncated JSON repair (SafeChain output buffer truncation).
    repaired = _repair_truncated_json(stripped)
    if repaired:
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            pass
    # Trailing-garbage / abandoned-object salvage. SafeChain has no constrained
    # decoding, so the model sometimes emits a valid JSON prefix then spills
    # into prose/markdown — e.g. `{"answer": "..."}` followed by markdown
    # bullets, or an object it abandons mid-way without closing. The decoder
    # reports the offset where valid JSON ended; truncate there, drop a
    # dangling comma, then balance any still-open braces/quotes.
    try:
        json.loads(stripped)
    except json.JSONDecodeError as exc:
        head = stripped[: exc.pos].rstrip()
        if head.endswith(","):
            head = head[:-1]
        salvaged = _repair_truncated_json(head)
        if salvaged:
            try:
                return json.loads(salvaged)
            except (json.JSONDecodeError, ValueError):
                pass
    return None
