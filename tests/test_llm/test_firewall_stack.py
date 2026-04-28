"""Tests for FirewallStack — the state container shared by FirewalledAsyncOpenAI."""

from __future__ import annotations

import asyncio

import pytest

from llm.firewall_stack import FirewallStack
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
