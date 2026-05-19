"""Tests for agents.chat_agent — merged ChatAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent_factories.chat_agent import ChatAgent
from logger.event_logger import EventLogger
from models.types import (
    DataPullRequest,
    FinalAnswer,
    LLMResult,
    ReportDraft,
    ScreenVerdict,
    TeamDraft,
)


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-chat", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


# ── screen() — composite: redact + relevance_check ─────────────────────────

async def test_screen_passes_in_scope_question(mock_llm, logger):
    # Question contains digits ("366132845011" looks like a case id) so it
    # bypasses the fast-path and BOTH redact + relevance_check run.
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"redacted": "redacted q", "masked_spans": []}),
        LLMResult(status="success", data={"passed": True, "reason": ""}),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("What's the bureau score for case 366132845011?")
    assert isinstance(verdict, ScreenVerdict)
    assert verdict.passed is True
    assert verdict.reason == ""
    assert verdict.redacted_question == "redacted q"


async def test_screen_rejects_off_topic(mock_llm, logger):
    # "what should I eat" is short + no digits → fast-path skips redact;
    # only the relevance_check LLM call fires.
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"passed": False, "reason": "Off-topic — case review only."}),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("What should I eat for lunch?")
    assert verdict.passed is False
    assert "case review" in verdict.reason.lower()
    # Redact was skipped → redacted_question is the input verbatim.
    assert verdict.redacted_question == "What should I eat for lunch?"


async def test_screen_fast_path_skips_redact_for_trivial_question(mock_llm, logger):
    """Short, plainly-non-sensitive questions skip the redact LLM call —
    saves ~1-2s of latency on trivial out-of-scope inputs like "what to
    eat" / "hi". Only the relevance_check call should fire."""
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"passed": False, "reason": "Out of scope."}),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("what to eat")
    assert verdict.passed is False
    # Exactly ONE LLM call (relevance), not two (redact + relevance).
    assert mock_llm.ainvoke.call_count == 1


async def test_screen_redact_runs_when_text_could_carry_pii(mock_llm, logger):
    """Anything that LOOKS like it might carry a case ID / SSN / phone /
    email still goes through redact — even if short."""
    # Has a digit run of 3+ → triggers redact.
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"redacted": "case <ID>", "masked_spans": []}),
        LLMResult(status="success", data={"passed": True, "reason": ""}),
    ])
    agent = ChatAgent(mock_llm, logger)
    await agent.screen("case 366132845011")
    assert mock_llm.ainvoke.call_count == 2  # redact + relevance

    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"redacted": "user@<EMAIL>", "masked_spans": []}),
        LLMResult(status="success", data={"passed": True, "reason": ""}),
    ])
    await agent.screen("user@example.com is angry")
    assert mock_llm.ainvoke.call_count == 2


async def test_screen_redact_blocked_falls_through(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="blocked", data=None, error="firewall hit"),
        LLMResult(status="success", data={"passed": True, "reason": ""}),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("any question")
    assert verdict.passed is True
    assert verdict.redacted_question == "any question"


async def test_screen_relevance_blocked_fails_open(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"redacted": "q", "masked_spans": []}),
        LLMResult(status="blocked", data=None, error="firewall hit"),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("anything")
    assert verdict.passed is True


# ── redact() — public ──────────────────────────────────────────────────────

async def test_redact_returns_redacted_text(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"redacted": "card ***MASKED***", "masked_spans": ["4532123456789"]},
    ))
    agent = ChatAgent(mock_llm, logger)
    result = await agent.redact("card 4532123456789")
    assert result == "card ***MASKED***"


async def test_redact_blocked_returns_input_unchanged(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="blocked", data=None, error="x",
    ))
    agent = ChatAgent(mock_llm, logger)
    result = await agent.redact("raw text")
    assert result == "raw text"


# ── relevance_check() — public ─────────────────────────────────────────────

async def test_relevance_check_returns_passed_reason_tuple(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"passed": False, "reason": "off-topic"},
    ))
    agent = ChatAgent(mock_llm, logger)
    passed, reason, near_dup, near_dup_reason = await agent.relevance_check("anything")
    assert passed is False
    assert reason == "off-topic"
    assert near_dup == ""
    assert near_dup_reason == ""


async def test_relevance_check_blocked_fails_open(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="blocked", data=None, error="x",
    ))
    agent = ChatAgent(mock_llm, logger)
    passed, reason, near_dup, near_dup_reason = await agent.relevance_check("q")
    assert passed is True
    assert reason == ""
    assert near_dup == ""
    assert near_dup_reason == ""


async def test_relevance_check_emits_near_duplicate_when_prior_matches(mock_llm, logger):
    """When the LLM names a prior question verbatim, near_duplicate_of is surfaced."""
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={
            "passed": True,
            "reason": "",
            "near_duplicate_of": "What is the customer's spending pattern?",
            "near_duplicate_reason": "Same subject, no time narrowing.",
        },
    ))
    agent = ChatAgent(mock_llm, logger)
    passed, reason, near_dup, near_dup_reason = await agent.relevance_check(
        "Show me the customer's spending pattern.",
        prior_questions=["What is the customer's spending pattern?"],
    )
    assert passed is True
    assert near_dup == "What is the customer's spending pattern?"
    assert "Same subject" in near_dup_reason


async def test_relevance_check_drops_hallucinated_near_duplicate(mock_llm, logger):
    """When the LLM cites a prior question that wasn't actually in the list,
    the wrapper drops the claim defensively (avoids replaying a bogus cache hit).
    """
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={
            "passed": True,
            "reason": "",
            "near_duplicate_of": "Some made-up prior question",
            "near_duplicate_reason": "matches",
        },
    ))
    agent = ChatAgent(mock_llm, logger)
    passed, reason, near_dup, near_dup_reason = await agent.relevance_check(
        "current question",
        prior_questions=["A real prior question that doesn't match"],
    )
    assert passed is True
    assert near_dup == ""
    assert near_dup_reason == ""


# ── format() — output ──────────────────────────────────────────────────────

def _final(data_pull_request=None, flags=None):
    return FinalAnswer(
        answer="test answer",
        flags=flags or [],
        report_draft=ReportDraft(coverage="implicit"),
        team_draft=TeamDraft(answer="team answer", specialists_consulted=["bureau"]),
        data_pull_request=data_pull_request,
    )


def test_format_renders_basic_answer():
    final = FinalAnswer(
        answer="The credit risk is moderate.",
        flags=["team confirms report"],
        report_draft=ReportDraft(coverage="explicit", files_consulted=["bureau.md"]),
        team_draft=TeamDraft(answer="t", specialists_consulted=["bureau", "spend_payments"]),
    )
    formatted = ChatAgent.format(final)
    assert "credit risk is moderate" in formatted
    assert "bureau" in formatted
    assert "spend_payments" in formatted
    assert "Report coverage: explicit" in formatted
    assert "team confirms report" in formatted


def test_format_omits_flags_section_when_empty():
    final = _final(flags=[])
    formatted = ChatAgent.format(final)
    assert "\n## Flags" not in formatted


def test_format_without_pull_request_omits_section():
    formatted = ChatAgent.format(_final())
    assert "Data pull recommendation" not in formatted


def test_format_with_pull_request_renders_section():
    dpr = DataPullRequest(
        needed=True,
        reason="Need bureau refresh",
        would_pull=["bureau.fico_latest"],
        severity="high",
    )
    formatted = ChatAgent.format(_final(data_pull_request=dpr))
    assert "Data pull recommendation (severity: high)" in formatted
    assert "Need bureau refresh" in formatted
    assert "bureau.fico_latest" in formatted
    assert "No live pull today" in formatted


def test_format_with_needed_false_omits_section():
    dpr = DataPullRequest(needed=False, reason="ok", would_pull=[], severity="low")
    formatted = ChatAgent.format(_final(data_pull_request=dpr))
    assert "Data pull recommendation" not in formatted


def test_format_with_empty_would_pull_shows_placeholder():
    dpr = DataPullRequest(
        needed=True, reason="generic concern", would_pull=[], severity="low",
    )
    formatted = ChatAgent.format(_final(data_pull_request=dpr))
    assert "Would pull: (nothing specific flagged)" in formatted


def test_format_final_answer_alias_works():
    """Backwards-compat: the old method name still resolves to format()."""
    final = _final()
    assert ChatAgent.format_final_answer(final) == ChatAgent.format(final)


# ── converse() ─────────────────────────────────────────────────────────────

async def test_converse_returns_response(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"response": "The bureau score indicates moderate risk."},
    ))
    agent = ChatAgent(mock_llm, logger)
    response = await agent.converse("What does the bureau score mean?", context="Score is 680")
    assert isinstance(response, str)
    assert len(response) > 0
    assert "bureau score" in response.lower()


async def test_converse_forwards_tools_to_llm(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(status="success", data={"response": "ok"}))

    def fake_helper(term: str) -> str:
        """Fake helper doc."""
        return term

    agent = ChatAgent(mock_llm, logger, tools=[fake_helper])
    await agent.converse("What is DTI?")
    call_kwargs = mock_llm.ainvoke.await_args.kwargs
    assert call_kwargs.get("tools") == [fake_helper]


async def test_converse_no_tools_passes_none(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(status="success", data={"response": "ok"}))
    agent = ChatAgent(mock_llm, logger)
    await agent.converse("Hi")
    call_kwargs = mock_llm.ainvoke.await_args.kwargs
    assert call_kwargs.get("tools") is None
