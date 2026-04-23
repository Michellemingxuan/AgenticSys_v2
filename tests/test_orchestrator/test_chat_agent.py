"""Tests for orchestrator.chat_agent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from logger.event_logger import EventLogger
from models.types import FinalOutput, LLMResult
from orchestrator.chat_agent import ChatAgent


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-chat", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


def test_format_for_reviewer(mock_llm, logger):
    final = FinalOutput(
        answer="The credit risk is moderate based on bureau and spend data.",
        specialists_consulted=["bureau", "spend_payments"],
    )

    agent = ChatAgent(mock_llm, logger)
    formatted = agent.format_for_reviewer(final)

    assert "credit risk is moderate" in formatted
    assert "bureau" in formatted
    assert "spend_payments" in formatted
    assert "Specialists consulted" in formatted


async def test_converse_returns_response(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(
            status="success",
            data={"response": "The bureau score indicates moderate risk."},
        )
    )

    agent = ChatAgent(mock_llm, logger)
    response = await agent.converse("What does the bureau score mean?", context="Score is 680")

    assert isinstance(response, str)
    assert len(response) > 0
    assert "bureau score" in response.lower()


async def test_converse_forwards_tools_to_llm(mock_llm, logger):
    """When ChatAgent is constructed with helper tools, every converse()
    LLM call should receive them via the tools= kwarg on ainvoke."""
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(status="success", data={"response": "ok"})
    )

    def fake_helper(term: str) -> str:
        """Fake helper doc."""
        return term

    agent = ChatAgent(mock_llm, logger, tools=[fake_helper])
    await agent.converse("What is DTI?")

    call_kwargs = mock_llm.ainvoke.await_args.kwargs
    assert call_kwargs.get("tools") == [fake_helper]


async def test_converse_no_tools_passes_none(mock_llm, logger):
    """Default ChatAgent (no tools) forwards tools=None — preserves legacy behavior."""
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(status="success", data={"response": "ok"})
    )

    agent = ChatAgent(mock_llm, logger)
    await agent.converse("Hi")

    call_kwargs = mock_llm.ainvoke.await_args.kwargs
    assert call_kwargs.get("tools") is None
