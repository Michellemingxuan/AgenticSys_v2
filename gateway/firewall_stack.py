"""Firewall retry stack — wraps every LLM call with content-safety retries."""

from __future__ import annotations

import re

from gateway.llm_adapter import BaseLLMAdapter
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
    """Wraps every LLM call with firewall retry logic."""

    def __init__(
        self,
        adapter: BaseLLMAdapter,
        logger: EventLogger,
        max_retries: int = 2,
    ):
        self.adapter = adapter
        self.logger = logger
        self.max_retries = max_retries
        self.step_history: list[StepRecord] = []

    def call(
        self,
        system_prompt: str,
        user_message: str,
        tools: list | None = None,
        output_type=None,
    ) -> LLMResult:
        attempt = 0
        current_system = system_prompt
        current_message = user_message

        while attempt <= self.max_retries:
            try:
                result = self.adapter.run(
                    system_prompt=current_system,
                    user_message=current_message,
                    tools=tools,
                    output_type=output_type,
                )
                record = StepRecord(
                    prompt=current_system,
                    message=current_message,
                    result=result,
                    attempt=attempt,
                )
                self.step_history.append(record)
                return LLMResult(status="success", data=result)
            except FirewallRejection as e:
                self.logger.log(
                    "firewall_rejection",
                    {"code": e.code, "message": e.message, "attempt": attempt},
                )
                attempt += 1
                if attempt > self.max_retries:
                    self.logger.log(
                        "firewall_blocked",
                        {"code": e.code, "message": e.message, "attempts": attempt},
                    )
                    return LLMResult(status="blocked", error=str(e))
                # Add guidance and sanitize for retry
                current_system = system_prompt + "\n\n" + FIREWALL_GUIDANCE
                current_message = self._sanitize_message(user_message)

        # Should not reach here, but safety fallback
        return LLMResult(status="blocked", error="max retries exhausted")  # pragma: no cover

    def rollback_to(self, step_index: int) -> None:
        """Truncate step_history to the given index."""
        self.step_history = self.step_history[:step_index]

    @staticmethod
    def _sanitize_message(message: str) -> str:
        """Mask long digit sequences (6+ digits) with ***MASKED***."""
        return re.sub(r"\d{6,}", "***MASKED***", message)
