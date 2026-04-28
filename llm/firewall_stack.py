"""Firewall stack — content-safety helpers and shared state for the OpenAI Agents SDK path."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from pydantic import BaseModel

from logger.event_logger import EventLogger


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
    """Shared state container for the firewall layer.

    Holds the logger, max-retry count, and concurrency semaphore that
    ``FirewalledAsyncOpenAI`` uses for retry-with-guidance and concurrent
    request capping.  No LLM call logic lives here — that moved to
    ``FirewalledAsyncOpenAI`` (llm/firewall_client.py).
    """

    def __init__(
        self,
        logger: EventLogger,
        max_retries: int = 2,
        concurrency_cap: int = 8,
    ):
        self.logger = logger
        self.max_retries = max_retries
        self.semaphore = asyncio.Semaphore(concurrency_cap)
