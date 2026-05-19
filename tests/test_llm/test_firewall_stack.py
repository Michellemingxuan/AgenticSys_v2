"""Tests for FirewallStack — the state container shared by FirewalledAsyncOpenAI."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from llm.firewall_stack import FirewallStack, LLM_CALL_KIND
from logger.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-fw", log_dir=str(tmp_path))


def test_construct_with_only_logger(logger):
    fw = FirewallStack(logger=logger)
    assert fw.logger is logger
    assert fw.max_retries == 2
    assert isinstance(fw.semaphore, asyncio.Semaphore)


def test_max_retries_overridable(logger):
    fw = FirewallStack(logger=logger, max_retries=5)
    assert fw.max_retries == 5


def test_firewall_stack_holds_state(logger):
    fw = FirewallStack(logger, max_retries=3, concurrency_cap=5)
    assert fw.max_retries == 3
    assert isinstance(fw.semaphore, asyncio.Semaphore)
    assert fw.logger is logger


# ── New two-tier concurrency + gate() coverage ──────────────────────────────


def test_default_caps_separate_orchestrator_and_specialist_pools(logger):
    """Defaults are 4 (orch) and 8 (specialist); the two semaphores
    must be distinct objects (each call kind has its own slot pool)."""
    fw = FirewallStack(logger=logger)
    assert fw.orchestrator_cap == 4
    assert fw.specialist_cap == 8
    assert fw.orchestrator_semaphore is not fw.specialist_semaphore
    # Back-compat: `.semaphore` aliases the specialist pool.
    assert fw.semaphore is fw.specialist_semaphore


def test_env_overrides_caps(monkeypatch, logger):
    """`FIREWALL_SPECIALIST_CONCURRENCY` / `FIREWALL_ORCH_CONCURRENCY`
    env vars override the defaults."""
    monkeypatch.setenv("FIREWALL_SPECIALIST_CONCURRENCY", "12")
    monkeypatch.setenv("FIREWALL_ORCH_CONCURRENCY", "6")
    fw = FirewallStack(logger=logger)
    assert fw.specialist_cap == 12
    assert fw.orchestrator_cap == 6


def test_concurrency_cap_fallback_for_backcompat(logger):
    """Callers that still pass the old single `concurrency_cap=N` get
    that value for BOTH pools (no env override). Preserves the prior
    strict-cap behavior for callers wired before the split."""
    fw = FirewallStack(logger=logger, concurrency_cap=2)
    assert fw.specialist_cap == 2
    assert fw.orchestrator_cap == 2


def test_gate_routes_orchestrator_kind_to_orchestrator_pool(logger):
    """Default LLM_CALL_KIND is 'orchestrator'; `gate()` should
    acquire the orchestrator semaphore."""
    fw = FirewallStack(logger=logger)
    orch_before = fw.orchestrator_semaphore._value
    spec_before = fw.specialist_semaphore._value

    async def use():
        async with fw.gate():
            # While held, orchestrator pool is down by 1; specialist
            # untouched.
            assert fw.orchestrator_semaphore._value == orch_before - 1
            assert fw.specialist_semaphore._value == spec_before

    asyncio.run(use())


def test_gate_routes_specialist_kind_to_specialist_pool(logger):
    """When `LLM_CALL_KIND` is set to 'specialist' (typically by
    redacting_tool), `gate()` acquires the specialist semaphore."""
    fw = FirewallStack(logger=logger)
    orch_before = fw.orchestrator_semaphore._value
    spec_before = fw.specialist_semaphore._value

    async def use():
        tok = LLM_CALL_KIND.set("specialist")
        try:
            async with fw.gate():
                assert fw.specialist_semaphore._value == spec_before - 1
                assert fw.orchestrator_semaphore._value == orch_before
        finally:
            LLM_CALL_KIND.reset(tok)

    asyncio.run(use())


def test_gate_logs_when_wait_exceeds_threshold():
    """Saturation diagnostic: when an LLM call waits ≥100ms for a slot,
    `firewall_semaphore_wait` is logged with the kind + cap so future
    tuning has evidence rather than guess."""
    events: list[tuple[str, dict]] = []
    fake_logger = SimpleNamespace(log=lambda ev, payload: events.append((ev, payload)))

    fw = FirewallStack(
        logger=fake_logger,  # type: ignore[arg-type]
        specialist_concurrency=1,  # tight cap to force contention
        orchestrator_concurrency=1,
    )

    async def hold(ms: int) -> None:
        tok = LLM_CALL_KIND.set("specialist")
        try:
            async with fw.gate():
                await asyncio.sleep(ms / 1000)
        finally:
            LLM_CALL_KIND.reset(tok)

    async def main():
        # First call takes the only slot; second waits ≥200ms.
        await asyncio.gather(hold(200), hold(200))

    asyncio.run(main())
    wait_events = [
        p for ev, p in events if ev == "firewall_semaphore_wait"
    ]
    assert len(wait_events) == 1, f"expected exactly one wait log, got {events}"
    assert wait_events[0]["kind"] == "specialist"
    assert wait_events[0]["waited_ms"] >= 100
    assert wait_events[0]["cap"] == 1
