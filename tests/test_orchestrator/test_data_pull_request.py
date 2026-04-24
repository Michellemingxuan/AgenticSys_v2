"""Tests for Orchestrator.balance's DataPullRequest parsing + flag prepend."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.types import LLMResult, ReportDraft, TeamDraft
from orchestrator.orchestrator import Orchestrator


def test_parse_data_pull_request_valid():
    raw = {
        "needed": True,
        "reason": "Missing bureau refresh",
        "would_pull": ["bureau.fico_latest_90d"],
        "severity": "medium",
    }
    dpr = Orchestrator._parse_data_pull_request(raw)
    assert dpr is not None
    assert dpr.needed is True
    assert dpr.severity == "medium"


def test_parse_data_pull_request_none_on_non_dict():
    assert Orchestrator._parse_data_pull_request(None) is None
    assert Orchestrator._parse_data_pull_request("x") is None


def test_parse_data_pull_request_coerces_bad_severity():
    raw = {"needed": True, "reason": "x", "would_pull": [], "severity": "bogus"}
    dpr = Orchestrator._parse_data_pull_request(raw)
    assert dpr is not None
    assert dpr.severity == "low"


def test_parse_data_pull_request_filters_non_string_items():
    raw = {
        "needed": True, "reason": "x",
        "would_pull": ["good", {"bad": True}, 42, None, "also good"],
        "severity": "high",
    }
    dpr = Orchestrator._parse_data_pull_request(raw)
    assert dpr.would_pull == ["good", "42", "also good"]


@pytest.mark.asyncio
async def test_balance_attaches_pull_request_and_prepends_flag():
    logger = MagicMock()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={
            "answer": "merged answer",
            "flags": ["existing flag"],
            "data_pull_request": {
                "needed": True,
                "reason": "missing stuff",
                "would_pull": ["stuff"],
                "severity": "high",
            },
        },
    ))
    orchestrator = Orchestrator(
        mock_llm, logger, MagicMock(), "credit_risk", pillar_config={}, catalog=None,
    )

    report = ReportDraft(coverage="none")
    team = TeamDraft(answer="team draft")
    final = await orchestrator.balance("q?", report, team)

    assert final.data_pull_request is not None
    assert final.data_pull_request.needed is True
    assert final.flags[0] == "data insufficient — pull recommended"
    logger.log.assert_any_call("data_pull_requested", {
        "would_pull": ["stuff"], "severity": "high", "reason": "missing stuff",
    })


@pytest.mark.asyncio
async def test_balance_no_pull_request_when_field_absent():
    logger = MagicMock()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"answer": "merged answer", "flags": []},
    ))
    orchestrator = Orchestrator(
        mock_llm, logger, MagicMock(), "credit_risk", pillar_config={}, catalog=None,
    )

    report = ReportDraft(coverage="full", answer="r")
    team = TeamDraft(answer="t")
    final = await orchestrator.balance("q?", report, team)

    assert final.data_pull_request is None
    assert "data insufficient — pull recommended" not in final.flags


@pytest.mark.asyncio
async def test_balance_no_flag_prepend_when_needed_false():
    logger = MagicMock()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={
            "answer": "merged answer",
            "flags": [],
            "data_pull_request": {
                "needed": False,
                "reason": "ok",
                "would_pull": [],
                "severity": "low",
            },
        },
    ))
    orchestrator = Orchestrator(
        mock_llm, logger, MagicMock(), "credit_risk", pillar_config={}, catalog=None,
    )

    report = ReportDraft(coverage="full", answer="r")
    team = TeamDraft(answer="t")
    final = await orchestrator.balance("q?", report, team)

    # The DPR is still attached (with needed=False) but no flag is prepended.
    assert final.data_pull_request is not None
    assert final.data_pull_request.needed is False
    assert "data insufficient — pull recommended" not in final.flags


def test_balance_fallback_has_no_pull_request():
    report = ReportDraft(coverage="full", answer="r")
    team = TeamDraft(answer="t")
    fallback = Orchestrator._balance_fallback(report, team)
    assert fallback.data_pull_request is None
