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
import json
import os
import time
import uuid
from typing import Any

from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
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


_ROLE_LABELS = {
    "system": "Context",
    "user": "Request",
    "assistant": "Response",
    "tool": "Tool result",
}


class SafeChainAsyncOpenAI:
    """Drop-in for ``openai.AsyncOpenAI`` that calls SafeChain underneath."""

    def __init__(self, *, model_name: str, firewall: FirewallStack):
        self._model_name = model_name
        self._firewall = firewall
        self._llm: Any = None  # lazy-initialised on first use
        self.chat = _SafeChainChat(self)

    def __getattr__(self, name: str):
        # Endpoints the SDK doesn't actually use under chat completions
        # (responses, files, embeddings) raise on access — surface a clear
        # error if someone tries to use them.
        raise AttributeError(
            f"SafeChainAsyncOpenAI does not expose '{name}'. Only chat "
            f"completions are routed through SafeChain."
        )

    def _ensure_llm(self) -> Any:
        if self._llm is None:
            self._refresh_llm()
        return self._llm

    def _refresh_llm(self) -> None:
        """(Re)load the safechain model. Used on first call and on 401 retry."""
        try:
            from safechain.lcel import model as safechain_model  # type: ignore[import-not-found]
        except ImportError as e:
            raise NotImplementedError(
                "safechain is not installed in this environment. "
                "SafeChainAsyncOpenAI is only usable in the private/prod env."
            ) from e
        model_id = os.environ.get("SAFECHAIN_MODEL", self._model_name)
        self._llm = safechain_model(model_id)


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
        **kw: Any,
    ) -> ChatCompletion:
        firewall = self._parent._firewall
        attempt = 0
        # Pre-redact every outbound message (mirrors FirewalledAsyncOpenAI).
        messages = [_redact_message(m) for m in messages]
        while True:
            try:
                async with firewall.semaphore:
                    return await self._invoke(
                        model=model,
                        messages=messages,
                        tools=tools,
                        response_format=response_format,
                    )
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
    ) -> ChatCompletion:
        combined = _combine_messages(messages, tools, response_format)
        try:
            from safechain.prompts import ValidChatPromptTemplate  # type: ignore[import-not-found]
        except ImportError as e:
            raise NotImplementedError(
                "safechain is not installed — SafeChainAsyncOpenAI is for "
                "the private/prod environment only."
            ) from e

        llm = self._parent._ensure_llm()

        def _sync_invoke() -> str:
            chain = ValidChatPromptTemplate.from_messages(
                [("human", "{__input__}")]
            ) | llm
            r = chain.invoke({"__input__": combined})
            return r.content if hasattr(r, "content") else str(r)

        try:
            text = await asyncio.to_thread(_sync_invoke)
        except Exception as e:  # noqa: BLE001 — we re-classify below
            es = str(e)
            if "401" in es:
                # Token expiry — refresh and retry once.
                self._parent._refresh_llm()
                text = await asyncio.to_thread(_sync_invoke)
            elif "403" in es:
                raise FirewallRejection("403", f"safechain blocked: {es}")
            elif "400" in es:
                raise FirewallRejection("400", f"safechain bad request: {es}")
            else:
                raise

        return _synthesize_chat_completion(text=text, model=model)


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
) -> str:
    """Flatten a multi-turn message list into a single string with neutral
    role labels (``Context``, ``Request``, ``Response``, ``Tool result``) and
    optionally append the tool-schema instructions when ``tools`` is set."""
    parts: list[str] = []
    tool_block_appended = False
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        if role == "system" and tools and not tool_block_appended:
            content = content + "\n\n" + _build_tool_schema_block(tools)
            tool_block_appended = True
        if role == "system" and response_format is not None and not tool_block_appended:
            # If there are no tools but a response_format is required, surface it.
            content = content + "\n\n" + _build_response_format_hint(response_format)
        label = _ROLE_LABELS.get(role, role.title() or "Context")
        parts.append(f"{label}:\n{content}")
    if tools and not tool_block_appended:
        # No system message present; prepend the tool block at the top.
        parts.insert(0, f"Context:\n{_build_tool_schema_block(tools)}")
    return "\n\n".join(parts)


def _build_tool_schema_block(tools: list[dict]) -> str:
    """Render the SDK's tool definitions as a text block the LLM can read.

    The SDK passes tools as OpenAI-style dicts with a ``function`` field that
    holds ``name``, ``description``, and ``parameters`` (JSON schema). We just
    render them in a stable text format and tell the LLM how to emit
    ``{"tool_call": …}`` / ``{"output": …}`` JSON.
    """
    lines = [
        "You have access to the following tools.",
        "To call a tool, respond with ONLY this JSON (no other text):",
        '  {"tool_call": {"name": "<tool_name>", "arguments": {<args>}}}',
        "",
        "When you have the final structured answer, respond with ONLY this JSON:",
        '  {"output": {<your structured answer matching output_schema>}}',
        "",
        "Available tools:",
    ]
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or t
        name = fn.get("name", "?")
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


def _synthesize_chat_completion(*, text: str, model: str) -> ChatCompletion:
    """Translate SafeChain's text response into an OpenAI ``ChatCompletion``.

    Three cases:
    - text parses as ``{"tool_call": {"name": ..., "arguments": ...}}`` →
      synthesise a ``ChatCompletion`` with ``tool_calls=[…]`` so the SDK
      sees a tool-call response.
    - text parses as ``{"output": {...}}`` → synthesise with the wrapped
      structured answer as ``content``.
    - otherwise → return text as ``content`` verbatim.
    """
    parsed = _try_parse_json(text)

    tool_calls: list[ChatCompletionMessageToolCall] | None = None
    content: str | None = text
    finish_reason: str = "stop"

    if isinstance(parsed, dict) and "tool_call" in parsed:
        tc = parsed["tool_call"]
        if isinstance(tc, dict):
            args = tc.get("arguments", tc.get("args", {}))
            args_json = args if isinstance(args, str) else json.dumps(args or {})
            tool_calls = [
                ChatCompletionMessageToolCall(
                    id=f"call_{uuid.uuid4().hex[:24]}",
                    type="function",
                    function=Function(name=str(tc.get("name", "")), arguments=args_json),
                )
            ]
            content = None
            finish_reason = "tool_calls"
    elif isinstance(parsed, dict) and "output" in parsed:
        out = parsed["output"]
        content = out if isinstance(out, str) else json.dumps(out)

    message = ChatCompletionMessage(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    choice = Choice(index=0, message=message, finish_reason=finish_reason)
    return ChatCompletion(
        id=f"chatcmpl_{uuid.uuid4().hex[:24]}",
        choices=[choice],
        created=int(time.time()),
        model=model,
        object="chat.completion",
    )


def _try_parse_json(text: str) -> Any:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Sometimes the LLM wraps JSON in markdown fences. Strip them.
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
            return None
