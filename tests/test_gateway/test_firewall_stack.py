"""Tests for gateway.firewall_stack."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.firewall_stack import FirewallRejection, FirewallStack, FIREWALL_GUIDANCE
from logger.event_logger import EventLogger
from models.types import LLMResult


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def mock_adapter():
    return MagicMock()


def test_successful_call(mock_adapter, logger):
    mock_adapter.run.return_value = {"answer": "42"}
    fw = FirewallStack(mock_adapter, logger)
    result = fw.call("system", "hello")
    assert result.status == "success"
    assert result.data == {"answer": "42"}
    assert len(fw.step_history) == 1


def test_retry_on_firewall_rejection(mock_adapter, logger):
    mock_adapter.run.side_effect = [
        FirewallRejection("PII", "contains PII"),
        {"answer": "safe"},
    ]
    fw = FirewallStack(mock_adapter, logger)
    result = fw.call("system", "hello")
    assert result.status == "success"
    assert result.data == {"answer": "safe"}
    assert mock_adapter.run.call_count == 2


def test_exhausted_retries(mock_adapter, logger):
    mock_adapter.run.side_effect = FirewallRejection("PII", "always blocked")
    fw = FirewallStack(mock_adapter, logger, max_retries=2)
    result = fw.call("system", "hello")
    assert result.status == "blocked"
    assert "always blocked" in result.error


def test_step_history_tracks_success(mock_adapter, logger):
    mock_adapter.run.return_value = {"ok": True}
    fw = FirewallStack(mock_adapter, logger)
    fw.call("s1", "m1")
    fw.call("s2", "m2")
    assert len(fw.step_history) == 2


def test_rollback(mock_adapter, logger):
    mock_adapter.run.return_value = {"ok": True}
    fw = FirewallStack(mock_adapter, logger)
    fw.call("s1", "m1")
    fw.call("s2", "m2")
    fw.call("s3", "m3")
    assert len(fw.step_history) == 3
    fw.rollback_to(1)
    assert len(fw.step_history) == 1


def test_firewall_guidance_added_on_retry(mock_adapter, logger):
    captured_prompts = []

    def capture_run(system_prompt, user_message, **kwargs):
        captured_prompts.append(system_prompt)
        if len(captured_prompts) == 1:
            raise FirewallRejection("INJ", "injection detected")
        return {"safe": True}

    mock_adapter.run.side_effect = capture_run
    fw = FirewallStack(mock_adapter, logger)
    result = fw.call("original system", "hello")
    assert result.status == "success"
    assert len(captured_prompts) == 2
    assert FIREWALL_GUIDANCE not in captured_prompts[0]
    assert FIREWALL_GUIDANCE in captured_prompts[1]
