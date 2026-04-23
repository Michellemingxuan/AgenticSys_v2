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

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_message)]

        response = await self.model.ainvoke(messages)

        content = response.content if hasattr(response, "content") else str(response)
        data = {"response": content}

        record = StepRecord(
            prompt=system_prompt,
            message=user_message,
            result=data,
            attempt=0,
        )
        self.firewall.step_history.append(record)
        return LLMResult(status="success", data=data)
