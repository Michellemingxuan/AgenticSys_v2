"""Tests for FirewallStack — owns config + step_history + sanitize utilities."""

from __future__ import annotations

import asyncio

import pytest

from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import StepRecord


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-fw", log_dir=str(tmp_path))


def test_construct_with_only_logger(logger):
    fw = FirewallStack(logger=logger)
    assert fw.logger is logger
    assert fw.max_retries == 2
    assert fw.step_history == []


def test_max_retries_overridable(logger):
    fw = FirewallStack(logger=logger, max_retries=5)
    assert fw.max_retries == 5


def test_step_history_append_and_rollback(logger):
    fw = FirewallStack(logger=logger)
    fw.step_history.append(StepRecord(prompt="p1", message="m1", result={}, attempt=0))
    fw.step_history.append(StepRecord(prompt="p2", message="m2", result={}, attempt=0))
    fw.step_history.append(StepRecord(prompt="p3", message="m3", result={}, attempt=0))
    assert len(fw.step_history) == 3

    fw.rollback_to(1)
    assert len(fw.step_history) == 1
    assert fw.step_history[0].prompt == "p1"


def test_sanitize_message_masks_long_digit_runs(logger):
    fw = FirewallStack(logger=logger)
    assert fw._sanitize_message("acct 1234567 test") == "acct ***MASKED*** test"
    assert fw._sanitize_message("short 12345 ok") == "short 12345 ok"


def test_sanitize_message_masks_case_id_tokens(logger):
    fw = FirewallStack(logger=logger)
    out = fw._sanitize_message("From CASE-00042: payment 1234567890")
    assert "CASE-00042" not in out
    assert "[CASE-ID]" in out
    assert "***MASKED***" in out


async def test_send_logs_and_redacts_plain_string(logger):
    fw = FirewallStack(logger=logger)
    out = await fw.send(
        "acct 4532123456789 CASE-00001",
        from_agent="X",
        to_agent="Y",
    )
    assert "4532123456789" not in out
    assert "CASE-00001" not in out


async def test_send_redacts_pydantic_model_fields(logger):
    """Send round-trips a Pydantic model, preserving type but masking string fields."""
    from models.types import ReportDraft

    fw = FirewallStack(logger=logger)
    draft = ReportDraft(
        coverage="full",
        answer="Row from CASE-00042 with acct 4532123456789",
        evidence_excerpts=['"CASE-00042 flagged"'],
        files_consulted=["bureau.md"],
    )
    out = await fw.send(draft, from_agent="report_agent", to_agent="orchestrator")

    assert isinstance(out, ReportDraft)
    assert out.coverage == "full"
    assert "CASE-00042" not in out.answer
    assert "4532123456789" not in out.answer
    assert "CASE-00042" not in out.evidence_excerpts[0]
    # Short strings (filenames) untouched.
    assert out.files_consulted == ["bureau.md"]


async def test_send_walks_dict_and_list(logger):
    fw = FirewallStack(logger=logger)
    payload = {
        "question": "acct 4532123456789",
        "flags": ["CASE-00001 issue", "no problem"],
    }
    out = await fw.send(payload, from_agent="a", to_agent="b")
    assert "4532123456789" not in out["question"]
    assert "CASE-00001" not in out["flags"][0]
    assert out["flags"][1] == "no problem"


async def test_semaphore_bounds_concurrent_ainvoke(logger):
    """With concurrency_cap=2, 4 parallel ainvoke calls complete without
    deadlock and never exceed 2 in-flight bound_model.ainvoke calls at once."""
    from unittest.mock import AsyncMock, MagicMock
    from langchain_core.messages import AIMessage

    fw = FirewallStack(logger=logger, concurrency_cap=2)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow_ainvoke(messages):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return AIMessage(content="ok")

    model = MagicMock()
    model.ainvoke = slow_ainvoke
    model.bind_tools = MagicMock(return_value=model)

    fwm = fw.wrap(model)
    results = await asyncio.gather(*(fwm.ainvoke("s", "u") for _ in range(4)))

    assert all(r.status == "success" for r in results)
    assert peak <= 2, f"semaphore allowed {peak} concurrent calls, expected ≤ 2"
