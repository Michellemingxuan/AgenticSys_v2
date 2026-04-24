"""Tests for orchestrator.chat_agent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from logger.event_logger import EventLogger
from models.types import (
    DataPullRequest,
    FinalAnswer,
    LLMResult,
    ReportDraft,
    TeamDraft,
)
from orchestrator.chat_agent import ChatAgent


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-chat", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


def test_format_final_answer(mock_llm, logger):
    final = FinalAnswer(
        answer="The credit risk is moderate based on bureau and spend data.",
        flags=["team confirms report"],
        report_draft=ReportDraft(coverage="full", files_consulted=["bureau.md"]),
        team_draft=TeamDraft(
            answer="team answer",
            specialists_consulted=["bureau", "spend_payments"],
        ),
    )

    formatted = ChatAgent.format_final_answer(final)

    assert "credit risk is moderate" in formatted
    assert "bureau" in formatted
    assert "spend_payments" in formatted
    assert "Report coverage: full" in formatted
    assert "team confirms report" in formatted


def test_format_final_answer_no_flags(mock_llm, logger):
    """When there are no flags, the Flags section is omitted."""
    final = FinalAnswer(
        answer="clean answer",
        flags=[],
        report_draft=ReportDraft(coverage="none"),
        team_draft=TeamDraft(answer="t", specialists_consulted=["bureau"]),
    )

    formatted = ChatAgent.format_final_answer(final)

    assert "\n## Flags" not in formatted
    assert "Report coverage: none" in formatted


def _final_with_dpr(dpr):
    return FinalAnswer(
        answer="test answer",
        flags=[],
        report_draft=ReportDraft(coverage="partial"),
        team_draft=TeamDraft(answer="team answer", specialists_consulted=["bureau"]),
        data_pull_request=dpr,
    )


def test_format_without_pull_request_omits_section():
    formatted = ChatAgent.format_final_answer(_final_with_dpr(None))
    assert "Data pull recommendation" not in formatted


def test_format_with_pull_request_renders_section():
    dpr = DataPullRequest(
        needed=True,
        reason="Need bureau refresh",
        would_pull=["bureau.fico_latest", "spend_payments.returned_reasons"],
        severity="high",
    )
    formatted = ChatAgent.format_final_answer(_final_with_dpr(dpr))
    assert "Data pull recommendation (severity: high)" in formatted
    assert "Need bureau refresh" in formatted
    assert "bureau.fico_latest" in formatted
    assert "spend_payments.returned_reasons" in formatted
    assert "No live pull today" in formatted


def test_format_with_needed_false_omits_section():
    dpr = DataPullRequest(needed=False, reason="ok", would_pull=[], severity="low")
    formatted = ChatAgent.format_final_answer(_final_with_dpr(dpr))
    assert "Data pull recommendation" not in formatted


def test_format_with_empty_would_pull_shows_placeholder():
    dpr = DataPullRequest(
        needed=True, reason="generic concern", would_pull=[], severity="low",
    )
    formatted = ChatAgent.format_final_answer(_final_with_dpr(dpr))
    assert "Would pull: (nothing specific flagged)" in formatted


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
