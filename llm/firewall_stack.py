"""Firewall stack — content-safety helpers and shared state for the OpenAI Agents SDK path."""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from pydantic import BaseModel

from logger.event_logger import EventLogger


# Two-tier concurrency: orchestrator-driven LLM calls (team-planning,
# synthesis, general_specialist review) use a separate slot pool from
# specialist-driven calls (every tool round-trip inside a specialist
# agent's Runner.run). Without this split, a Round-1 burst of 3
# specialists × 4-6 internal LLM calls each pile up behind a single
# small semaphore, serializing what should be parallel work and adding
# tens of seconds per turn.
#
# The ContextVar is set to "specialist" inside `redacting_tool` around
# the inner Runner.run; everywhere else it defaults to "orchestrator".
# asyncio Tasks inherit the ContextVar context naturally, and the
# semaphore acquire happens INSIDE the contextvar scope (before
# asyncio.to_thread in the safechain client) so the right pool is
# always selected.
LLM_CALL_KIND: contextvars.ContextVar[str] = contextvars.ContextVar(
    "LLM_CALL_KIND", default="orchestrator",
)


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


# How long an LLM call can wait for a semaphore slot before we log it.
# A nonzero wait isn't a problem per se — under load some queueing is
# expected — but anything over this threshold is worth surfacing so we
# can see whether the concurrency caps are the binding constraint.
_SEMAPHORE_WAIT_LOG_THRESHOLD_MS = 100


class FirewallStack:
    """Shared state container for the firewall layer.

    Holds the logger, max-retry count, and the TWO concurrency
    semaphores (orchestrator-priority + specialist-priority) that the
    LLM clients route through. No LLM call logic lives here — that's
    in ``FirewalledAsyncOpenAI`` / ``SafeChainAsyncOpenAI``. Both
    clients call ``async with firewall.gate():`` instead of acquiring
    a semaphore directly, so the kind-routing + wait-time
    instrumentation lives in exactly one place.

    Cap defaults (env-overridable):
      - `FIREWALL_SPECIALIST_CONCURRENCY` (default 8) — specialist
        tool round-trips. Higher value because a typical turn has
        12-18 specialist LLM calls; with the old cap of 3 they
        serialized into 4-6 sequential batches and dominated turn
        wall-clock.
      - `FIREWALL_ORCH_CONCURRENCY` (default 4) — orchestrator
        team-planning + synthesis + general_specialist review.
        Smaller pool because the orchestrator is sparse (2-3 calls
        per turn) and reserving slots for it ensures its calls
        don't get queued behind a specialist storm.

    For OpenAI on a strict rate-limit tier (e.g. 30K TPM): set
    `FIREWALL_SPECIALIST_CONCURRENCY=3 FIREWALL_ORCH_CONCURRENCY=2`
    to restore the pre-fix tight cap. For safechain / private env,
    the defaults give roughly 3-4× the prior concurrency.
    """

    def __init__(
        self,
        logger: EventLogger,
        max_retries: int = 2,
        # `concurrency_cap` kept for backward compat with callers
        # constructing FirewallStack with the old single-semaphore
        # signature. When set, it's used for BOTH pools unless the
        # env vars override; this preserves the prior strict-cap
        # behavior for existing callers without changing their args.
        concurrency_cap: int | None = None,
        specialist_concurrency: int | None = None,
        orchestrator_concurrency: int | None = None,
    ):
        self.logger = logger
        self.max_retries = max_retries

        # Resolve caps: env wins, then explicit kwarg, then
        # `concurrency_cap` fallback (for back-compat), then default.
        def _cap(env_name: str, explicit: int | None, default: int) -> int:
            env_val = os.environ.get(env_name)
            if env_val is not None:
                try:
                    return max(1, int(env_val))
                except ValueError:
                    pass
            if explicit is not None:
                return max(1, explicit)
            if concurrency_cap is not None:
                return max(1, concurrency_cap)
            return default

        self.specialist_cap = _cap(
            "FIREWALL_SPECIALIST_CONCURRENCY", specialist_concurrency, 8,
        )
        self.orchestrator_cap = _cap(
            "FIREWALL_ORCH_CONCURRENCY", orchestrator_concurrency, 4,
        )
        self.specialist_semaphore = asyncio.Semaphore(self.specialist_cap)
        self.orchestrator_semaphore = asyncio.Semaphore(self.orchestrator_cap)
        # Back-compat alias so any caller still reaching for
        # `firewall.semaphore` directly resolves to the specialist
        # pool (the larger of the two; tighter to break than the
        # orchestrator pool).
        self.semaphore = self.specialist_semaphore

    @asynccontextmanager
    async def gate(self) -> AsyncIterator[None]:
        """Pick the right semaphore based on the LLM_CALL_KIND
        ContextVar and acquire it. Used by both
        `FirewalledAsyncOpenAI` and `SafeChainAsyncOpenAI` in place
        of the prior `async with self._firewall.semaphore:` so the
        kind-routing + wait-time instrumentation lives in one place.
        """
        kind = LLM_CALL_KIND.get()
        sem = (
            self.orchestrator_semaphore if kind == "orchestrator"
            else self.specialist_semaphore
        )
        t0 = time.perf_counter()
        async with sem:
            waited_ms = int((time.perf_counter() - t0) * 1000)
            # Log only meaningful waits — under load this surfaces
            # whether the cap is the binding constraint. A typical
            # acquire is sub-millisecond when slots are free.
            if waited_ms >= _SEMAPHORE_WAIT_LOG_THRESHOLD_MS:
                try:
                    self.logger.log("firewall_semaphore_wait", {
                        "kind": kind,
                        "waited_ms": waited_ms,
                        "cap": (
                            self.orchestrator_cap if kind == "orchestrator"
                            else self.specialist_cap
                        ),
                    })
                except Exception:
                    # Logger failure must never break an LLM call.
                    pass
            yield
