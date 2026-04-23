"""Tests for FirewallStack — owns config + step_history + sanitize utilities."""

from __future__ import annotations

import pytest

from gateway.firewall_stack import FirewallStack
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
