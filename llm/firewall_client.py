"""FirewalledAsyncOpenAI — wraps openai.AsyncOpenAI with PII redaction,
retry-with-guidance on FirewallRejection, and a shared concurrency cap."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

from llm.firewall_stack import (
    FIREWALL_GUIDANCE,
    FirewallRejection,
    FirewallStack,
    forces_tool_call,
    sanitize_message,
)


def _redact_message(message: dict) -> dict:
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": sanitize_message(content)}
    return message


def _inject_guidance(messages: list[dict]) -> list[dict]:
    """Append firewall guidance to the system message; resanitize all messages."""
    out = []
    appended = False
    for m in messages:
        m = _redact_message(m)
        if not appended and m.get("role") == "system":
            m = {**m, "content": (m.get("content") or "") + "\n\n" + FIREWALL_GUIDANCE}
            appended = True
        out.append(m)
    return out


def _render_messages_for_excerpt(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        parts.append(f"[{role}] {content}")
    return "\n\n".join(parts)


class _FirewalledChatCompletions:
    def __init__(self, base_completions: Any, firewall: FirewallStack):
        self._base = base_completions
        self._firewall = firewall

    async def create(self, *, model, messages, **kw):
        # A round that MUST call a tool cannot emit the final structured
        # answer, so drop the schema — 6.8 KB of it, on the orchestrator's
        # dispatch round, describes fields the model is told to leave null.
        # Mirrored from the safechain path so a dev measurement means something
        # about prod (openai/safechain parity). See the "Per-round payload
        # shaping" section of llm/firewall_stack.py.
        if kw.get("response_format") is not None and forces_tool_call(
                kw.get("tool_choice")):
            kw = {k: v for k, v in kw.items() if k != "response_format"}
        # Imported lazily to avoid a top-level cycle once node_trace grows.
        from tools.node_trace import (
            ACTIVE_NODE, NodeTrace, _hooks_own_rounds,
            attach_io, attach_latency, attach_usage,
        )
        from tools.node_trace.pricing import compute_cost

        messages = [_redact_message(m) for m in messages]
        attempt = 0
        while True:
            try:
                async with self._firewall.gate():
                    parent = ACTIVE_NODE.get()
                    if parent is None or parent.row_id <= 0:
                        # Not inside a traced scope — pass through with no telemetry.
                        return await self._base.create(
                            model=model, messages=messages, **kw,
                        )
                    if _hooks_own_rounds(parent):
                        # The SDK RunHooks-based path (NodeTraceRunHooks)
                        # is creating round children for this parent.
                        # Wrapping again here would double-count rows
                        # AND produce partial-data rows for streamed
                        # responses where resp.model_dump_json() can't
                        # be called. Pass through; the hook captures.
                        return await self._base.create(
                            model=model, messages=messages, **kw,
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
                        prompt_text = _render_messages_for_excerpt(messages)
                        sys_chars = sum(
                            len(m.get("content") or "")
                            for m in messages if m.get("role") == "system"
                        )
                        attach_usage(
                            prompt_excerpt=prompt_text,
                            system_prompt_chars=sys_chars or None,
                            model=model,
                        )
                        attach_io(messages_json=json.dumps(messages, default=str))
                        _llm_t0 = perf_counter()
                        resp = await self._base.create(
                            model=model, messages=messages, **kw,
                        )
                        attach_latency(llm_call_ms=int((perf_counter() - _llm_t0) * 1000))

                        usage = getattr(resp, "usage", None)
                        if usage is not None:
                            p_tok = getattr(usage, "prompt_tokens", None)
                            c_tok = getattr(usage, "completion_tokens", None)
                            t_tok = getattr(usage, "total_tokens", None)
                            pdet = getattr(usage, "prompt_tokens_details", None)
                            cached = None
                            if pdet is not None:
                                cached = (
                                    getattr(pdet, "cached_tokens", None)
                                    if not isinstance(pdet, dict)
                                    else pdet.get("cached_tokens")
                                )
                            cdet = getattr(usage, "completion_tokens_details", None)
                            reasoning = None
                            if cdet is not None:
                                reasoning = (
                                    getattr(cdet, "reasoning_tokens", None)
                                    if not isinstance(cdet, dict)
                                    else cdet.get("reasoning_tokens")
                                )
                            cost = compute_cost(
                                model=model,
                                prompt_tokens=p_tok,
                                cached_input_tokens=cached,
                                completion_tokens=c_tok,
                            )
                            attach_usage(
                                prompt_tokens=p_tok,
                                completion_tokens=c_tok,
                                total_tokens=t_tok,
                                cached_input_tokens=cached,
                                reasoning_tokens=reasoning,
                                cost_usd=cost,
                            )
                        try:
                            completion_text = (
                                resp.choices[0].message.content or ""
                                if getattr(resp, "choices", None) else ""
                            )
                        except Exception:
                            completion_text = ""
                        attach_usage(completion_excerpt=completion_text)
                        try:
                            out_json = resp.model_dump_json() if hasattr(resp, "model_dump_json") else None
                        except Exception:
                            out_json = None
                        if out_json is not None:
                            attach_io(output_json=out_json)
                        return resp
            except FirewallRejection as e:
                self._firewall.logger.log("firewall_rejection",
                                          {"code": e.code, "message": e.message,
                                           "attempt": attempt})
                if attempt >= self._firewall.max_retries:
                    self._firewall.logger.log("firewall_blocked",
                                              {"code": e.code, "message": e.message,
                                               "attempts": attempt + 1})
                    raise
                attempt += 1
                messages = _inject_guidance(messages)


class _FirewalledChat:
    def __init__(self, base_chat: Any, firewall: FirewallStack):
        self.completions = _FirewalledChatCompletions(base_chat.completions, firewall)


class FirewalledAsyncOpenAI:
    """Drop-in replacement for openai.AsyncOpenAI used by the Agents SDK."""

    def __init__(self, base: Any, firewall: FirewallStack):
        self._base = base
        self._firewall = firewall
        self.chat = _FirewalledChat(base.chat, firewall)

    def __getattr__(self, name: str):
        return getattr(self._base, name)
