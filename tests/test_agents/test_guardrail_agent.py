"""Tests for agents.guardrail_agent.GuardrailAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.guardrail_agent import GuardrailAgent
from logger.event_logger import EventLogger
from models.types import GuardrailVerdict, LLMResult


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-guard", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


def _redact_response(redacted: str, masked: list[str] | None = None) -> LLMResult:
    return LLMResult(
        status="success",
        data={"redacted": redacted, "masked_spans": masked or []},
    )


def _relevance_response(passed: bool, reason: str = "") -> LLMResult:
    return LLMResult(status="success", data={"passed": passed, "reason": reason})


async def test_screen_in_scope_question_passes(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            _redact_response("What is the bureau score?"),
            _relevance_response(True),
        ]
    )
    agent = GuardrailAgent(mock_llm, logger)
    verdict = await agent.screen("What is the bureau score?")

    assert isinstance(verdict, GuardrailVerdict)
    assert verdict.passed is True
    assert verdict.reason == ""
    assert verdict.redacted_question == "What is the bureau score?"


async def test_screen_off_topic_question_rejected(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            _redact_response("what should I eat for lunch?"),
            _relevance_response(
                False,
                "This system only answers questions about the current credit-risk case.",
            ),
        ]
    )
    agent = GuardrailAgent(mock_llm, logger)
    verdict = await agent.screen("what should I eat for lunch?")

    assert verdict.passed is False
    assert "credit-risk case" in verdict.reason
    assert verdict.redacted_question == "what should I eat for lunch?"


async def test_screen_redacts_digit_run_in_passed_question(mock_llm, logger):
    """Redact step masks the 6+-digit account number; redacted question threads through."""
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            _redact_response(
                "Show payments for account ***MASKED*** this month",
                masked=["4532123456789"],
            ),
            _relevance_response(True),
        ]
    )
    agent = GuardrailAgent(mock_llm, logger)
    verdict = await agent.screen("Show payments for account 4532123456789 this month")

    assert verdict.passed is True
    assert "***MASKED***" in verdict.redacted_question
    assert "4532123456789" not in verdict.redacted_question


async def test_screen_redacts_case_id_token(mock_llm, logger):
    """CASE-XXXXX tokens get replaced with [CASE-ID] by the redact skill."""
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            _redact_response(
                "Why was [CASE-ID] flagged?",
                masked=["CASE-00042"],
            ),
            _relevance_response(True),
        ]
    )
    agent = GuardrailAgent(mock_llm, logger)
    verdict = await agent.screen("Why was CASE-00042 flagged?")

    assert verdict.passed is True
    assert "[CASE-ID]" in verdict.redacted_question
    assert "CASE-00042" not in verdict.redacted_question


async def test_screen_redact_blocked_falls_through_with_raw_question(mock_llm, logger):
    """If the redact LLM call is blocked, the raw question reaches the relevance step."""
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            LLMResult(status="blocked", error="denied"),
            _relevance_response(True),
        ]
    )
    agent = GuardrailAgent(mock_llm, logger)
    verdict = await agent.screen("What is the FICO?")

    assert verdict.passed is True
    assert verdict.redacted_question == "What is the FICO?"
    assert mock_llm.ainvoke.call_count == 2


async def test_screen_relevance_blocked_fails_open(mock_llm, logger):
    """If the relevance LLM call is blocked, the verdict fails open (passed=True).

    Deliberate: a reviewer asking a legitimate question shouldn't be stonewalled
    because the guardrail LLM hiccupped. The firewall layer handles real content
    violations; guardrail is a quality gate, not a safety gate.
    """
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            _redact_response("What is the FICO?"),
            LLMResult(status="blocked", error="denied"),
        ]
    )
    agent = GuardrailAgent(mock_llm, logger)
    verdict = await agent.screen("What is the FICO?")

    assert verdict.passed is True
    assert verdict.redacted_question == "What is the FICO?"
