"""Tests for orchestrator.chat_agent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import FinalOutput, LLMResult
from orchestrator.chat_agent import ChatAgent


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-chat", log_dir=str(tmp_path))


@pytest.fixture
def firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter, logger)


def test_format_for_reviewer(firewall, logger):
    final = FinalOutput(
        answer="The credit risk is moderate based on bureau and spend data.",
        specialists_consulted=["bureau", "spend_payments"],
    )

    agent = ChatAgent(firewall, logger)
    formatted = agent.format_for_reviewer(final)

    assert "credit risk is moderate" in formatted
    assert "bureau" in formatted
    assert "spend_payments" in formatted
    assert "Specialists consulted" in formatted


def test_converse_returns_response(firewall, logger):
    firewall.call = MagicMock(
        return_value=LLMResult(
            status="success",
            data={"response": "The bureau score indicates moderate risk."},
        )
    )

    agent = ChatAgent(firewall, logger)
    response = agent.converse("What does the bureau score mean?", context="Score is 680")

    assert isinstance(response, str)
    assert len(response) > 0
    assert "bureau score" in response.lower()
