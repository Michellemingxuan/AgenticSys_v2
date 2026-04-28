"""Firewall stack — wraps every LLM call (LangChain models) with content-safety retries."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from logger.event_logger import EventLogger
from models.types import LLMResult, StepRecord


_CASE_ID_RE = re.compile(r"CASE-\d+")
_DIGIT_RUN_RE = re.compile(r"\d{6,}")


FIREWALL_GUIDANCE = (
    "[IMPORTANT: Your previous response was blocked by the content firewall. "
    "Avoid: raw account numbers, PII, role-injection patterns like [SYSTEM] or "
    "[USER], code execution keywords (exec, eval, import). Use masked identifiers "
    "and descriptive language instead of raw numeric values.]"
)


def sanitize_message(message: str) -> str:
    """Mask identifiers: long digit runs (6+ digits) and CASE-\\d+ tokens."""
    masked = _CASE_ID_RE.sub("[CASE-ID]", message)
    return _DIGIT_RUN_RE.sub("***MASKED***", masked)


def redact_payload(payload: Any) -> Any:
    if isinstance(payload, str):
        return sanitize_message(payload)
    if isinstance(payload, BaseModel):
        dumped = payload.model_dump()
        redacted = redact_payload(dumped)
        return type(payload).model_validate(redacted)
    if isinstance(payload, dict):
        return {k: redact_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(redact_payload(v) for v in payload)
    return payload


class FirewallRejection(Exception):
    """Raised when a firewall rule blocks an LLM response."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"FirewallRejection({code}): {message}")


class FirewallStack:
    """Owns firewall config, step history, and the inter-agent transit bus.

    Two chokepoints:
      - `wrap(model).ainvoke(...)` — every LLM call (retry on FirewallRejection,
        tool-loop, output_type parsing) passes through here.
      - `send(message, from, to)`  — every inter-agent transit runs through
        here so redact patterns apply on every cross-agent edge, not just at
        the LLM boundary.

    `concurrency_cap` bounds simultaneous LLM requests across all wrapped
    models. Parallel fan-out (Phase 4 asyncio.gather on Reports + Team, and
    parallel specialist dispatch) can otherwise trip OpenAI rate limits on
    large pillars. Default 8 is a safe starting point.
    """

    def __init__(
        self,
        logger: EventLogger,
        max_retries: int = 2,
        concurrency_cap: int = 8,
    ):
        self.logger = logger
        self.max_retries = max_retries
        self.concurrency_cap = concurrency_cap
        self.step_history: list[StepRecord] = []
        self.semaphore = asyncio.Semaphore(concurrency_cap)

    def wrap(self, model: BaseChatModel) -> "FirewalledModel":
        """Wrap a LangChain model with firewall retry logic."""
        return FirewalledModel(model=model, firewall=self)

    def rollback_to(self, step_index: int) -> None:
        """Truncate step_history to the given index."""
        self.step_history = self.step_history[:step_index]

    async def send(self, message: Any, from_agent: str, to_agent: str) -> Any:
        """Inter-agent transit chokepoint. Logs, redacts, and shape-validates.

        Returns the (possibly-redacted) message so callers can thread it into
        the next agent. Pydantic models round-trip through `model_dump` +
        `model_validate` so redaction applies to string fields without losing
        type information. Plain dicts, lists, and strings are walked in
        place. Other types pass through untouched.
        """
        self.logger.log(
            "firewall_send",
            {
                "from": from_agent,
                "to": to_agent,
                "type": type(message).__name__,
            },
        )
        return self._redact_payload(message)

    @classmethod
    def _redact_payload(cls, payload: Any) -> Any:
        return redact_payload(payload)

    @staticmethod
    def _sanitize_message(message: str) -> str:
        return sanitize_message(message)


class FirewalledModel:
    """LangChain chat model wrapped with retry-on-FirewallRejection + tool-call loop.

    Preserves the legacy `(system_prompt, user_message, tools, output_type) -> LLMResult`
    surface so call sites can migrate from `firewall.call(...)` to `await llm.ainvoke(...)`
    with minimal change.
    """

    def __init__(self, model: BaseChatModel, firewall: FirewallStack):
        self.model = model
        self.firewall = firewall

    async def ainvoke(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[Callable] | None = None,
        output_type: Any = None,
        max_tool_turns: int = 12,
    ) -> LLMResult:
        from langchain_core.messages import HumanMessage, SystemMessage

        from llm import case_scrubber

        attempt = 0
        current_system = system_prompt
        # Pre-step: case-ID scrubbing always happens, regardless of retry status.
        current_message = case_scrubber.scrub(user_message, case_id=None)

        bound_model = self.model.bind_tools(tools) if tools else self.model
        tool_map = {fn.__name__: fn for fn in (tools or [])}

        last_error: str | None = None

        while attempt <= self.firewall.max_retries:
            messages = [
                SystemMessage(content=current_system),
                HumanMessage(content=current_message),
            ]
            try:
                response = await self._tool_loop(bound_model, messages, tool_map, max_tool_turns)
            except FirewallRejection as e:
                self.firewall.logger.log(
                    "firewall_rejection",
                    {"code": e.code, "message": e.message, "attempt": attempt},
                )
                last_error = str(e)
                attempt += 1
                if attempt > self.firewall.max_retries:
                    self.firewall.logger.log(
                        "firewall_blocked",
                        {"code": e.code, "message": e.message, "attempts": attempt},
                    )
                    return LLMResult(status="blocked", error=last_error)
                # Re-prepare for the next attempt.
                current_system = system_prompt + "\n\n" + FIREWALL_GUIDANCE
                current_message = self.firewall._sanitize_message(
                    case_scrubber.scrub(user_message, case_id=None)
                )
                continue

            content = response.content if hasattr(response, "content") else str(response)

            if output_type is not None:
                import json as _json
                try:
                    parsed = _json.loads(content)
                    data = output_type(**parsed).model_dump()
                except Exception:
                    data = {"raw": content}
            else:
                data = {"response": content}

            record = StepRecord(
                prompt=current_system,
                message=current_message,
                result=data,
                attempt=attempt,
            )
            self.firewall.step_history.append(record)
            return LLMResult(status="success", data=data)

        # Unreachable in normal flow; safety fallback.
        return LLMResult(status="blocked", error=last_error or "max retries exhausted")  # pragma: no cover

    async def _tool_loop(
        self,
        bound_model,
        messages: list,
        tool_map: dict[str, Callable],
        max_tool_turns: int,
    ):
        """Drive bound_model until it returns a final non-tool AIMessage."""
        from langchain_core.messages import ToolMessage

        response = None
        for _ in range(max_tool_turns):
            # Shared semaphore caps concurrent LLM requests across all
            # FirewalledModel instances under this FirewallStack — protects
            # against rate limits on parallel fan-out (Reports + Team,
            # parallel specialists).
            async with self.firewall.semaphore:
                response = await bound_model.ainvoke(messages)
            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls:
                return response

            messages.append(response)
            for tc in tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else tc.name
                args = tc.get("args") if isinstance(tc, dict) else tc.args
                tc_id = tc.get("id") if isinstance(tc, dict) else tc.id
                fn = tool_map.get(name)
                if fn is None:
                    result = f"error: unknown tool {name}"
                else:
                    try:
                        result = str(fn(**(args or {})))
                    except Exception as exc:
                        result = f"error: {exc}"
                messages.append(ToolMessage(content=result, tool_call_id=tc_id))

        return response  # last response after exhausting turns
