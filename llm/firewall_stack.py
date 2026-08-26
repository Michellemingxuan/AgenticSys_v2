"""Firewall stack — the helpers and shared state BOTH LLM transports import.

Content safety (identifier masking, payload redaction), per-round payload
shaping, the rejection exception, and the two-tier concurrency gate. Anything
that has to behave identically on the OpenAI and safechain paths lives here,
so the two implementations cannot drift apart.
"""

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
# The ContextVar is set to "specialist" inside `agent_tool` around
# the inner Runner.run; everywhere else it defaults to "orchestrator".
# asyncio Tasks inherit the ContextVar context naturally, and the
# semaphore acquire happens INSIDE the contextvar scope (before
# asyncio.to_thread in the safechain client) so the right pool is
# always selected.
LLM_CALL_KIND: contextvars.ContextVar[str] = contextvars.ContextVar(
    "LLM_CALL_KIND", default="orchestrator",
)


_CASE_ID_RE = re.compile(r"CASE-\d+")
_DIGIT_RUN_RE = re.compile(r"\d{10,}")


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


# ---------------------------------------------------------------------------
# Per-round payload shaping
#
# One rule today: a round that MUST call a tool cannot emit the final structured
# answer, so sending `response_format` on it is dead weight.
#
# WHY IT IS WORTH A DECISION POINT. The SDK computes `response_format`
# unconditionally — `OpenAIChatCompletionsModel._fetch_response` has no branch on
# round number or tool state — so every round of an agent with an `output_type`
# carries the full schema. For the orchestrator that is 8,341 chars of
# `FinalAnswer` schema on round 1, whose only job is to emit tool calls under
# `tool_choice="required"`.
#
# On safechain the cost is not just bytes. The model is a langchain
# `AzureChatOpenAI`, and the presence of `response_format` is what routes the call
# through OpenAI's AUTO-PARSE path rather than plain `create` — the same
# mechanism behind "'make_chart' is not strict. Only 'strict' function can be
# auto-parsed". So a bare dispatch round pays for structured-output machinery it
# cannot use.
#
# SAFETY. `tool_choice="required"` (or a named-function choice) means the model
# must return a tool call, not content. There is no response for the schema to
# shape. If the model ignores that and returns prose anyway — a known safechain
# flake — the SDK raises `ModelBehaviorError` and the conductor's dispatch-skip
# retry handles it. That happens with or without `response_format`, since the
# prose would not parse either way; dropping it makes that path no worse.
#
# `auto` / `none` / unset are LEFT ALONE: those rounds may legitimately produce
# the final answer, and that is exactly when the schema earns its place.
#
# Applied in BOTH transports, per the openai/safechain parity rule — the openai
# path saves the bytes even though it never auto-parses, and keeping the two
# behaviours identical is what makes a dev measurement mean anything about prod.
# ---------------------------------------------------------------------------


def forces_tool_call(tool_choice: Any) -> bool:
    """True when this `tool_choice` obliges the model to emit a tool call.

    `"required"` and a named-function object both do. `"auto"`, `"none"`,
    `None` and the SDK's NOT_GIVEN sentinel do not. Unknown values are treated
    as NOT forcing, so an unrecognised choice keeps the schema rather than
    silently dropping it.
    """
    if isinstance(tool_choice, str):
        return tool_choice == "required"
    # Named-function form: {"type": "function", "function": {"name": …}}
    if isinstance(tool_choice, dict):
        return tool_choice.get("type") == "function"
    return False


def response_format_for_round(tool_choice: Any, response_format: Any) -> Any:
    """`response_format`, or None when this round cannot use it.

    The single decision point for the rule above, so the two transports cannot
    drift apart.
    """
    if response_format is None:
        return None
    return None if forces_tool_call(tool_choice) else response_format


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
            # Record onto the active node-trace if one is set. Telemetry
            # failures must never break an LLM call.
            try:
                from tools.node_trace import attach_latency
                attach_latency(queue_wait_ms=waited_ms)
            except Exception:
                pass
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
                    pass
            yield
