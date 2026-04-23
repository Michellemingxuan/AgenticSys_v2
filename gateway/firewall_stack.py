"""Firewall stack — wraps every LLM call (LangChain models) with content-safety retries."""

from __future__ import annotations

import re
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel

from logger.event_logger import EventLogger
from models.types import LLMResult, StepRecord


FIREWALL_GUIDANCE = (
    "[IMPORTANT: Your previous response was blocked by the content firewall. "
    "Avoid: raw account numbers, PII, role-injection patterns like [SYSTEM] or "
    "[USER], code execution keywords (exec, eval, import). Use masked identifiers "
    "and descriptive language instead of raw numeric values.]"
)


class FirewallRejection(Exception):
    """Raised when a firewall rule blocks an LLM response."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"FirewallRejection({code}): {message}")


class FirewallStack:
    """Owns firewall config and step history. Use `wrap(model)` to build a FirewalledModel."""

    def __init__(self, logger: EventLogger, max_retries: int = 2):
        self.logger = logger
        self.max_retries = max_retries
        self.step_history: list[StepRecord] = []

    def wrap(self, model: BaseChatModel) -> "FirewalledModel":
        """Wrap a LangChain model with firewall retry logic."""
        return FirewalledModel(model=model, firewall=self)

    def rollback_to(self, step_index: int) -> None:
        """Truncate step_history to the given index."""
        self.step_history = self.step_history[:step_index]

    @staticmethod
    def _sanitize_message(message: str) -> str:
        """Mask long digit sequences (6+ digits) with ***MASKED***."""
        return re.sub(r"\d{6,}", "***MASKED***", message)


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
    ) -> LLMResult:
        from langchain_core.messages import HumanMessage, SystemMessage

        from gateway import case_scrubber

        attempt = 0
        current_system = system_prompt
        # Pre-step: case-ID scrubbing always happens, regardless of retry status.
        current_message = case_scrubber.scrub(user_message, case_id=None)

        last_error: str | None = None

        while attempt <= self.firewall.max_retries:
            messages = [
                SystemMessage(content=current_system),
                HumanMessage(content=current_message),
            ]
            try:
                response = await self.model.ainvoke(messages)
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
