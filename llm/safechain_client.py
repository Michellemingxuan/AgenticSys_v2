"""SafeChainAsyncOpenAI — drop-in replacement for ``openai.AsyncOpenAI`` that
routes ``chat.completions.create()`` through SafeChain.

Same shape as :class:`llm.firewall_client.FirewalledAsyncOpenAI`, so the SDK's
``OpenAIChatCompletionsModel`` keeps working unchanged — only the underlying
HTTP transport differs.

**Where this is used.** Private/production environment, where direct OpenAI
access is blocked and all LLM traffic goes through the SafeChain pipeline.
In dev / this repo, ``backend="openai"`` keeps the existing
``FirewalledAsyncOpenAI`` path; ``backend="safechain"`` activates this shim.

**How it works.** The model ``safechain.core.model.amodel()`` returns is a real
LangChain chat model — ``SafeAzureChatOpenAI <- InputRedactor <-
OpenAIMiddleware <- AzureChatOpenAI <- BaseChatOpenAI`` — so it supports native
tool-calling, ``response_format``, ``tool_choice`` and genuine streaming, and
``.bind(**kwargs)`` forwards OpenAI-shaped kwargs verbatim to the endpoint.
This shim therefore passes the SDK's OWN payload straight through and converts
the reply back:

    ValidChatPromptTemplate.from_messages([MessagesPlaceholder("messages")])
        | model.bind(tools=…, tool_choice=…, response_format=…)

- ``ValidChatPromptTemplate`` stays in the chain for COMPLIANCE. Its only
  override is ``format_prompt`` (template-time), which a bare
  ``.ainvoke(messages)`` would skip; ``MessagesPlaceholder`` is what lets an
  arbitrary runtime message list flow through it. Content redaction itself
  lives in the model (``InputRedactor`` is in its MRO) and applies either way.
- Messages are sent as a real role-tagged list. Tool calls and tool results
  round-trip natively; ``tool_choice="required"`` is enforced by the provider.
- HTTP 401 → token expiry → refresh the safechain model and retry once.
  HTTP 403 / 400 → raise :class:`FirewallRejection` — the existing retry-with-
  guidance loop in ``_FirewalledChatCompletions`` semantics is replicated here.

**History.** Until 2026-07-29 this shim spoke a TEXT protocol: tool schemas
injected into the prompt, replies recovered by ~500 lines of JSON repair. That
was written against an older safechain build with no native function-calling,
and it was the source of the ``ModelBehaviorError`` class of failures. Every
capability above was measured in the private env with ``tools/safechain_probe.py``
before this rewrite — see ``.claude/memory/safechain_dual_environment.md``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
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
from openai.types.completion_usage import CompletionUsage

from llm.firewall_stack import (
    FIREWALL_GUIDANCE,
    LLM_CALL_KIND,
    FirewallRejection,
    FirewallStack,
    response_format_for_round,
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
# is built async (`await amodel(...)`, which does the token acquisition) and the
# chain is run via `ainvoke` (native async on this build). `asyncio.wait_for`
# bounds it and, because `ainvoke` is genuinely cancellable, a timeout actually
# aborts the in-flight request and frees the firewall semaphore + turn lock
# promptly — no lingering worker thread. (Previously the chain was invoked
# synchronously in a thread pool, where a timeout could NOT interrupt the call;
# that orphaned-thread pileup was the original "stuck at team construction"
# mechanism. See .claude/memory/safechain_async_and_thread_occupation.md.)
_SAFECHAIN_CALL_TIMEOUT_S = float(os.environ.get("SAFECHAIN_CALL_TIMEOUT_S", "180"))

# STALL-AND-RETRY. Measured in the private env: safechain calls do not run
# slow, they STALL. In one turn `distiller.bureau` returned in 5.8s while
# `distiller.modeling` — concurrent, same pool, same model — hung; and every
# stall that was allowed to finish landed in a narrow band:
#
#     orchestrator.round_2  125.98s ok      spend_payments  129.50s ok
#     spend_payments        129.83s ok      crossbu         130.67s ok
#
# while every failure died at its own phase fence, not at a natural duration
# (report_agent 100.01s twice, distiller.modeling 120.01s twice,
# the reviewer 25.00s). Normal calls in those same turns took 2-13s.
#
# So a call still running at ~40s is not working, it is wedged, and the bet is
# that the wedge belongs to that in-flight REQUEST rather than to the work —
# a fresh request has a fresh chance. Abandoning an attempt is safe here:
# `ainvoke` is genuinely cancellable on this build (verified in prod), so a
# cancelled attempt aborts rather than orphaning a thread.
#
# THE SECOND ATTEMPT MUST BE ABLE TO OUTLAST A STALL, which is what fixes the
# budget at 140 rather than something smaller. Escalating:
#
#     retry escapes the stall  ->  ~45s   (vs ~130s today)
#     retry stalls too         ->  ~170s  (40 + the stall riding out: an answer)
#     both stall past 140s     ->  TimeoutError at 180s elapsed
#
# THE FENCE IS 40s, NOT TIGHTER. Measured per-call latency is p99 7.5s and max
# 7.5s across 88 dev calls, and 2.1-5.6s in prod, so 40 is ~5x the observed
# worst case: it cannot abandon a healthy call, and it only governs how long a
# STALL burns before the retry. And the budget is 140s, NOT 80: stalls that
# resolve do so at 126-131s, so an 80s cap could never finish one — two short
# attempts would fail twice where one 140s attempt succeeds.
#
# Set SAFECHAIN_STALL_RETRY_S=0 to disable and fall back to one attempt.
_SAFECHAIN_STALL_RETRY_S = float(os.environ.get("SAFECHAIN_STALL_RETRY_S", "40"))

# Retained thread pool. No longer used for the LLM call itself (that is now
# `ainvoke`), but kept so the loop's default executor can optionally be pinned
# to it — `loop.set_default_executor(_SAFECHAIN_EXECUTOR)` — so safechain's brief
# internal redaction offload lands on a sized pool instead of the shared default.
_SAFECHAIN_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.environ.get("SAFECHAIN_THREAD_POOL", "32")),
    thread_name_prefix="safechain-invoke",
)




def _tag_active_node(tag: str, **extra: Any) -> None:
    """Mark the in-scope node_trace row so AgenticEval can see the stall.

    The JSONL log is for a human reading one session; the trace DB is what the
    eval harness reads (it injects NODE_TRACE_DB per target and scores from
    it). A stall recorded only in the log is invisible to every scored run, so
    both channels carry it.

    The active node here is the round the call belongs to — e.g.
    `specialist.modeling.round_1` — which is exactly the granularity eval needs
    to attribute a stall to an agent. Never raises: telemetry must not be able
    to break the call it is describing.
    """
    try:
        from tools.node_trace import attach_extra, attach_tag
        attach_tag(tag)
        if extra:
            attach_extra(**extra)
    except Exception:  # noqa: BLE001
        pass


def _estimate_tokens(text: str, model: str) -> int:
    """tiktoken estimate with a robust fallback. Safechain doesn't return
    usage objects, so this is the only token signal we have on that path.

    Delegates to `tools.node_trace.pricing.estimate_tokens`, which caches the
    encoder AND its unavailability. This used to call tiktoken directly on
    every round: tiktoken fetches its BPE file over the network on first use,
    and where that host is unreachable each call blocked until the socket
    timed out — twice per round, from inside a coroutine, pinning the event
    loop. See the note in `pricing.py`.
    """
    from tools.node_trace.pricing import estimate_tokens
    return estimate_tokens(text, model)


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
        # Sampling / budget knobs the agent factories set explicitly. Under the
        # old TEXT transport these were dropped on the floor with the rest of
        # `kw` — nothing could be forwarded, since the request was a prompt
        # string. Native binding can honor them, and two matter:
        #   • `max_tokens` — specialists raise it to 3000 because a 500-cap
        #     truncated a batch call mid-argument and the specialist then
        #     fabricated numbers around the broken tool result.
        #   • `parallel_tool_calls` — the orchestrator opts in explicitly; we
        #     should honor that rather than silently inherit the provider
        #     default (which happens to match, so behavior is unchanged today —
        #     but turning it OFF would have been a no-op, which is a trap).
        # Kept to an ALLOW-LIST: forwarding arbitrary SDK extras risks a 400
        # from a provider that doesn't know them.
        passthrough = {k: kw.pop(k, None) for k in
                       ("max_tokens", "parallel_tool_calls", "temperature",
                        "top_p", "seed", "stop")}
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
                            tool_choice=tool_choice, passthrough=passthrough,
                        )
                    if _hooks_own_rounds(parent):
                        return await self._invoke(
                            model=model, messages=messages, tools=tools,
                            response_format=response_format, stream=stream,
                            tool_choice=tool_choice, passthrough=passthrough,
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
                        # Telemetry only — the wire payload is the role-tagged
                        # message list, not this rendering.
                        combined = _prompt_excerpt(messages)
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
                            tool_choice=tool_choice, passthrough=passthrough,
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
                        # Prefer the provider's OWN counts when the model
                        # reported them (`AIMessage.usage_metadata` ->
                        # `resp.usage`). They're exact, and they account for the
                        # tool schemas / response_format that were actually
                        # billed — which an estimate over the rendered prompt
                        # cannot see. Falls back to the tiktoken estimate when
                        # the build doesn't supply usage, so telemetry never
                        # silently drops to zero. `attach_usage` overwrites, so
                        # the earlier estimated prompt_tokens is corrected here.
                        _usage = getattr(resp, "usage", None)
                        if _usage is not None:
                            p_tok = _usage.prompt_tokens or p_tok
                            c_tok = _usage.completion_tokens or 0
                        else:
                            c_tok = _estimate_tokens(completion_text, model)
                        attach_usage(
                            completion_excerpt=completion_text,
                            prompt_tokens=p_tok,
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
        passthrough: dict | None = None,
    ) -> Any:
        try:
            from safechain.prompts import ValidChatPromptTemplate  # type: ignore[import-not-found]
            from langchain_core.prompts import MessagesPlaceholder  # type: ignore[import-not-found]
        except ImportError as e:
            raise NotImplementedError(
                "safechain is not installed — SafeChainAsyncOpenAI is for "
                "the private/prod environment only."
            ) from e

        # Build the model ASYNC (`await amodel(...)` does the token acquisition),
        # cached + reused. Run the chain via `ainvoke`: this build's
        # `SafeAzureChatOpenAI._agenerate` is a genuine async path (verified in
        # the private env — cancellable in ~0s ON A FREE EVENT LOOP, and
        # cancellation ABORTS the in-flight request rather than orphaning a
        # worker thread). Read that caveat literally: `wait_for` cancels the
        # task and then AWAITS it, so a cancel lands no sooner than the loop is
        # free to run it. Any blocking call on the same loop — sync I/O, or a
        # hidden first-use download — makes calls slow AND unkillable at once,
        # which reads as "safechain is not cancellable" and is not. That cost
        # several rounds of misdiagnosis on 2026-08-25; see
        # .claude/memory/safechain_async_and_thread_occupation.md. That is the
        # fix for "stuck after rewind": the old sync `chain.invoke` in a thread
        # pool could not be interrupted, so a rewind/timeout left the thread
        # running its full 20-120s call, holding a pool slot and starving the
        # next turn. With `ainvoke`, `wait_for` and task cancellation actually
        # stop the work. See .claude/memory/safechain_async_and_thread_occupation.md.
        llm = await self._parent._aensure_llm()

        # Compliance: `ValidChatPromptTemplate` stays in the chain. Its only
        # override is `format_prompt`, which is template-time — a bare
        # `.ainvoke(messages)` would skip it. `MessagesPlaceholder` is what lets
        # an arbitrary runtime message list flow through a ChatPromptTemplate,
        # so we keep the firewall template AND get native transport. (Content
        # redaction itself lives in the model — `InputRedactor` is in its MRO —
        # so that applies either way.) Verified: probe check R3.
        lc_messages = _to_lc_messages(messages)
        bind_kwargs = _bind_kwargs(tools, tool_choice, response_format,
                                   extra=passthrough)

        def _chain(active_model: Any):
            bound = active_model.bind(**bind_kwargs) if bind_kwargs else active_model
            return (ValidChatPromptTemplate.from_messages(
                [MessagesPlaceholder("messages")]) | bound)

        async def _run(active_model: Any) -> Any:
            """One logical call, with a short first attempt (see
            `_SAFECHAIN_STALL_RETRY_S`). Streaming is untouched: it is
            consumed chunk-by-chunk elsewhere, and restarting a half-drained
            generator is a different problem from re-issuing a request."""
            # Clamp: the first attempt can never outlast the whole per-call
            # budget. Without this, lowering SAFECHAIN_CALL_TIMEOUT_S below the
            # stall fence is silently ignored — attempt 1 keeps waiting past the
            # limit the operator just set, which is the opposite of what they
            # asked for, and exactly what breaks when running the
            # `SAFECHAIN_CALL_TIMEOUT_S=30` diagnostic.
            first_s = min(_SAFECHAIN_STALL_RETRY_S, _SAFECHAIN_CALL_TIMEOUT_S)
            if first_s > 0:
                try:
                    return await asyncio.wait_for(
                        _chain(active_model).ainvoke({"messages": lc_messages}),
                        timeout=first_s,
                    )
                except asyncio.TimeoutError:
                    # Not a failure yet — one attempt looked wedged, so drop it
                    # and re-issue. Recorded in TWO places on purpose: the JSONL
                    # log for a human reading one session, and the node trace so
                    # AgenticEval can find it. Eval reads the trace DB, never the
                    # JSONL, so a stall that only reached the log would be
                    # invisible to every scored run.
                    _tag_active_node("stall_retry",
                                     stall_retry_after_s=first_s)
                    try:
                        self._parent._firewall.logger.log(
                            "safechain_call_stalled",
                            {"stalled_after_s": first_s,
                             "kind": LLM_CALL_KIND.get(),
                             "backend": "safechain"},
                        )
                    except Exception:  # noqa: BLE001 — telemetry is not the call
                        pass
            try:
                return await asyncio.wait_for(
                    _chain(active_model).ainvoke({"messages": lc_messages}),
                    timeout=_SAFECHAIN_CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # THE DATUM THAT DECIDES THE BUDGET. This fires only when a
                # RE-ISSUED request also timed out — i.e. the retry did not
                # escape the wedge. The budget is currently sized on the bet
                # that it does (see `safechain_call_s` in tuning.yaml); if this
                # count is non-trivial in prod, the bet is wrong and the budget
                # has to go back above the 126-131s stall plateau so a stalled
                # call can at least ride it out.
                if first_s > 0:
                    _tag_active_node("stall_retry_failed",
                                     stall_retry_after_s=first_s,
                                     retry_budget_s=_SAFECHAIN_CALL_TIMEOUT_S)
                    try:
                        self._parent._firewall.logger.log(
                            "safechain_retry_stalled",
                            {"first_attempt_s": first_s,
                             "retry_budget_s": _SAFECHAIN_CALL_TIMEOUT_S,
                             "kind": LLM_CALL_KIND.get(),
                             "backend": "safechain"},
                        )
                    except Exception:  # noqa: BLE001 — telemetry is not the call
                        pass
                raise

        async def _run_stream(active_model: Any):
            return _chain(active_model).astream({"messages": lc_messages})

        try:
            reply = await (_run_stream(llm) if stream else _run(llm))
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"safechain LLM call did not return within "
                f"{_SAFECHAIN_CALL_TIMEOUT_S:.0f}s"
            ) from e
        except Exception as e:  # noqa: BLE001 — we re-classify below
            es = str(e)
            if "401" in es:
                # Token expiry — rebuild the model and retry once. (Note:
                # safechain's own OpenAIMiddleware also refreshes tokens, so
                # this may be belt-and-braces; kept because it is cheap and the
                # failure it guards against is a whole dead turn.)
                await self._parent._arefresh_llm()
                refreshed = await self._parent._aensure_llm()
                try:
                    reply = await (_run_stream(refreshed) if stream
                                   else _run(refreshed))
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

        # `stream=True` (Runner.run_streamed) gets a REAL token stream — the
        # model's `astream` yields AIMessageChunks, including incremental
        # `tool_call_chunks`, so the SDK's ChatCmplStreamHandler accumulates
        # exactly as it would against OpenAI.
        if stream:
            return _SafeChainStream(agen=reply, model=model)
        return _completion_from_message(reply, model)


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


# ── native transport: OpenAI wire format <-> LangChain ──────────────────────
#
# The safechain model is a real LangChain chat model (`SafeAzureChatOpenAI <-
# InputRedactor <- OpenAIMiddleware <- AzureChatOpenAI <- BaseChatOpenAI`), so
# it supports native tool-calling, `response_format`, `tool_choice` and real
# streaming. `.bind(**kwargs)` forwards OpenAI-shaped kwargs verbatim to the
# API, which means this shim can hand the SDK's OWN payload straight through
# and convert the reply back — no text protocol, no JSON repair.
# Verified in the private env; see .claude/memory/safechain_dual_environment.md.


def _prompt_excerpt(messages: list[dict]) -> str:
    """Flat text rendering of the outbound messages, for TELEMETRY ONLY.

    The node trace wants a prompt excerpt and a token estimate. This is not
    what goes on the wire any more — messages are sent as a real role-tagged
    list — so nothing here can affect model behavior.
    """
    parts = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str) if content else ""
        parts.append(f"{m.get('role', '?')}: {content}")
    return "\n\n".join(parts)


def _to_lc_messages(messages: list[dict]) -> list:
    """OpenAI-wire message dicts -> LangChain message objects.

    Imported lazily: `langchain_core` ships with safechain in the private env
    and is absent in dev, so a module-level import would break dev collection.

    Tool-call round-trips matter here. An assistant message carrying
    `tool_calls` must come back as an AIMessage with LangChain-shaped
    `tool_calls` (`args` as a DICT, not a JSON string), and each tool result
    must become a ToolMessage bound by `tool_call_id` — otherwise the provider
    rejects the follow-up round for referencing an unknown call id.
    """
    from langchain_core.messages import (
        AIMessage, HumanMessage, SystemMessage, ToolMessage,
    )

    out: list = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str):
            content = "" if content is None else json.dumps(content, default=str)

        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "tool":
            out.append(ToolMessage(
                content=content,
                tool_call_id=m.get("tool_call_id") or m.get("id") or "",
            ))
        elif role == "assistant":
            lc_calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (json.JSONDecodeError, ValueError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                lc_calls.append({"name": fn.get("name") or "", "args": args,
                                 "id": tc.get("id") or "", "type": "tool_call"})
            out.append(AIMessage(content=content, tool_calls=lc_calls))
        else:
            out.append(HumanMessage(content=content))
    return out


def _bind_kwargs(tools: list[dict] | None, tool_choice: Any,
                 response_format: Any, extra: dict | None = None) -> dict:
    """The OpenAI-shaped kwargs to forward through `.bind()`.

    Passed through UNCHANGED — the SDK already emits exactly what the Azure
    endpoint expects. Only omitted-vs-None is normalized, since binding
    `tools=None` is not the same as not binding tools at all.

    NOTE `tool_choice`: Azure accepts only `none` / `auto` / `required` (or a
    named-function object). `"any"` is rejected with a 400; the SDK never
    sends it, but don't "helpfully" translate anything into it.
    """
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = tools
    if tool_choice is not None and tools:
        # tool_choice without tools is a 400 — drop it rather than fail the call.
        kwargs["tool_choice"] = tool_choice
    # A round that MUST call a tool cannot emit the final structured answer, so
    # the schema is dead weight — and on this transport its mere presence is
    # what routes the call through OpenAI's auto-parse instead of `create`.
    # See the "Per-round payload shaping" section of llm/firewall_stack.py.
    response_format = response_format_for_round(tool_choice, response_format)
    if response_format is not None:
        kwargs["response_format"] = response_format
    for k, v in (extra or {}).items():
        if v is None:
            continue
        # `parallel_tool_calls` is only valid alongside tools — same 400 as a
        # bare tool_choice. The orchestrator sets it on every round, including
        # the final synthesis round that has no tools left to call.
        if k == "parallel_tool_calls" and not tools:
            continue
        kwargs[k] = v
    return kwargs


def _content_text(content: Any) -> str:
    """AIMessage.content -> plain text. Content can be a list of parts on
    multimodal-capable models; join the text parts and ignore the rest."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "".join(parts)
    return "" if content is None else str(content)


def _sdk_tool_calls(message: Any) -> list[ChatCompletionMessageToolCall] | None:
    """LangChain `AIMessage.tool_calls` -> OpenAI `tool_calls`.

    LangChain hands back `args` as a parsed DICT; the OpenAI wire type wants a
    JSON STRING, which is what the SDK will json.loads back out.
    """
    raw = getattr(message, "tool_calls", None) or []
    calls = []
    for i, tc in enumerate(raw):
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or ""
        if not name:
            continue
        args = tc.get("args")
        arguments = args if isinstance(args, str) else json.dumps(args or {}, default=str)
        calls.append(ChatCompletionMessageToolCall(
            id=tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            type="function",
            function=Function(name=name, arguments=arguments),
        ))
    return calls or None


def _usage_from_message(message: Any) -> CompletionUsage | None:
    """LangChain `AIMessage.usage_metadata` -> OpenAI `usage`, when present.

    The old text transport had no usage object at all, so token counts were
    ESTIMATED with tiktoken. A real LangChain chat model reports the provider's
    own counts, which are exact and — unlike an estimate over the rendered
    prompt — correctly account for the tool schemas and response_format the
    provider actually billed for. Returns None when the build doesn't supply it,
    so the caller keeps its estimate rather than reporting zeros.
    """
    meta = getattr(message, "usage_metadata", None)
    if not isinstance(meta, dict):
        return None
    prompt = meta.get("input_tokens")
    completion = meta.get("output_tokens")
    if prompt is None and completion is None:
        return None
    prompt = int(prompt or 0)
    completion = int(completion or 0)
    return CompletionUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=int(meta.get("total_tokens") or (prompt + completion)),
    )


def _completion_from_message(message: Any, model: str) -> ChatCompletion:
    """LangChain `AIMessage` -> OpenAI `ChatCompletion`.

    This is the whole response path now. The SDK sees an ordinary
    ChatCompletion and does its own parsing/validation, so a schema violation
    surfaces as the SDK's own error instead of being papered over by a repair
    heuristic.
    """
    tool_calls = _sdk_tool_calls(message)
    content = _content_text(getattr(message, "content", ""))
    meta = getattr(message, "response_metadata", None) or {}
    finish_reason = meta.get("finish_reason") if isinstance(meta, dict) else None
    if tool_calls:
        finish_reason = "tool_calls"
    elif finish_reason not in ("stop", "length", "content_filter", "tool_calls"):
        finish_reason = "stop"

    return ChatCompletion(
        id=getattr(message, "id", None) or f"chatcmpl_{uuid.uuid4().hex[:24]}",
        choices=[Choice(
            index=0,
            message=ChatCompletionMessage(
                role="assistant",
                content=content or None,
                tool_calls=tool_calls,
            ),
            finish_reason=finish_reason,
        )],
        created=int(time.time()),
        model=model,
        object="chat.completion",
        usage=_usage_from_message(message),
    )


def _chunk_from_message_chunk(chunk: Any, model: str, chunk_id: str,
                              created: int) -> ChatCompletionChunk | None:
    """LangChain `AIMessageChunk` -> OpenAI `ChatCompletionChunk`.

    Streaming is REAL now (verified: 10 chunks, 6 carrying tool_call_chunks),
    so the SDK's stream handler accumulates incrementally exactly as it would
    against OpenAI. `tool_call_chunks` carry PARTIAL argument strings keyed by
    `index` — forward them as-is and let the handler reassemble; parsing them
    here would recreate the repair layer we just deleted.
    """
    delta_tool_calls = []
    for tc in getattr(chunk, "tool_call_chunks", None) or []:
        if not isinstance(tc, dict):
            continue
        args = tc.get("args")
        delta_tool_calls.append(ChoiceDeltaToolCall(
            index=tc.get("index") or 0,
            id=tc.get("id") or None,
            type="function",
            function=ChoiceDeltaToolCallFunction(
                name=tc.get("name") or None,
                arguments=args if isinstance(args, str) else None,
            ),
        ))

    text = _content_text(getattr(chunk, "content", ""))
    if not text and not delta_tool_calls:
        return None                      # nothing to forward for this chunk

    return ChatCompletionChunk(
        id=chunk_id,
        choices=[ChunkChoice(
            index=0,
            delta=ChoiceDelta(
                content=text or None,
                tool_calls=delta_tool_calls or None,
            ),
            finish_reason=None,
        )],
        created=created,
        model=model,
        object="chat.completion.chunk",
    )


class _SafeChainStream:
    """Async-iterable over the model's real token stream.

    The SDK treats any async-iterable that is NOT a ChatCompletion as a stream.
    Opens with a role delta and closes with a finish_reason terminator, which is
    what `ChatCmplStreamHandler` expects to bracket the body.
    """

    def __init__(self, *, agen, model: str) -> None:
        self._agen = agen
        self._model = model
        self._id = f"chatcmpl_{uuid.uuid4().hex[:24]}"
        self._created = int(time.time())
        self._sent_role = False
        self._saw_tool_call = False
        self._done = False

    def __aiter__(self) -> "_SafeChainStream":
        return self

    def _bare(self, delta: ChoiceDelta, finish_reason: str | None = None) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id=self._id,
            choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish_reason)],
            created=self._created,
            model=self._model,
            object="chat.completion.chunk",
        )

    async def __anext__(self) -> ChatCompletionChunk:
        if not self._sent_role:
            self._sent_role = True
            return self._bare(ChoiceDelta(role="assistant"))
        if self._done:
            raise StopAsyncIteration
        while True:
            # Bound the GAP BETWEEN CHUNKS, not the whole stream: a long answer
            # is legitimately slow, a stalled transport is not. Without this the
            # streaming path would have no timeout at all — the non-streaming
            # path's `wait_for` covers one await, and a stream is many.
            try:
                raw = await asyncio.wait_for(
                    self._agen.__anext__(), timeout=_SAFECHAIN_CALL_TIMEOUT_S)
            except StopAsyncIteration:
                self._done = True
                return self._bare(
                    ChoiceDelta(),
                    finish_reason="tool_calls" if self._saw_tool_call else "stop",
                )
            except asyncio.TimeoutError as e:
                self._done = True
                raise TimeoutError(
                    f"safechain stream stalled for more than "
                    f"{_SAFECHAIN_CALL_TIMEOUT_S:.0f}s between chunks"
                ) from e
            converted = _chunk_from_message_chunk(
                raw, self._model, self._id, self._created)
            if converted is None:
                continue
            if converted.choices[0].delta.tool_calls:
                self._saw_tool_call = True
            return converted

    async def close(self) -> None:
        self._done = True
        aclose = getattr(self._agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


















_log = logging.getLogger(__name__)




















