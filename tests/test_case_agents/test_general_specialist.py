"""Tests for agents.general_specialist."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from case_agents.general_specialist import GeneralSpecialist, COMPARE_SYSTEM_PROMPT
from logger.event_logger import EventLogger
from models.types import LLMResult, ReviewReport, SpecialistOutput


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    """A FirewalledModel stand-in with a mockable async ainvoke."""
    return AsyncMock()


def _make_output(domain: str, findings: str) -> SpecialistOutput:
    return SpecialistOutput(
        domain=domain,
        question="test question",
        mode="chat",
        findings=findings,
        evidence=["ev1"],
        implications=["imp1"],
    )


def test_general_specialist_creation(mock_llm, logger):
    gs = GeneralSpecialist(mock_llm, logger)
    assert gs.llm is mock_llm
    assert gs.logger is logger


def test_generate_pairs(mock_llm, logger):
    gs = GeneralSpecialist(mock_llm, logger)
    pairs = gs._generate_pairs(["bureau", "capacity"])
    assert len(pairs) == 1
    assert pairs[0] == ("bureau", "capacity")


def test_generate_pairs_three(mock_llm, logger):
    gs = GeneralSpecialist(mock_llm, logger)
    pairs = gs._generate_pairs(["bureau", "capacity", "spend"])
    assert len(pairs) == 3


async def test_compare_returns_review_report(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(
            status="success",
            data={
                "resolved": [
                    {
                        "pair": ["bureau", "capacity"],
                        "contradiction": "Score vs income mismatch",
                        "question_raised": "Is income overstated?",
                        "answer": "Income appears consistent with tax records",
                        "supporting_evidence": ["tax_record_2023"],
                        "conclusion": "No real contradiction",
                    }
                ],
                "open_conflicts": [],
                "cross_domain_insights": ["Both domains flag high leverage"],
            },
        )
    )

    gs = GeneralSpecialist(mock_llm, logger)
    outputs = {
        "bureau": _make_output("bureau", "Score is 580"),
        "capacity": _make_output("capacity", "DTI is 45%"),
    }
    report = await gs.compare(outputs, "What is the credit risk?")

    assert isinstance(report, ReviewReport)
    assert len(report.resolved) == 1
    assert report.resolved[0].pair == ("bureau", "capacity")
    assert report.resolved[0].contradiction == "Score vs income mismatch"
    assert len(report.open_conflicts) == 0
    assert "Both domains flag high leverage" in report.cross_domain_insights


async def test_compare_single_specialist(mock_llm, logger):
    gs = GeneralSpecialist(mock_llm, logger)
    outputs = {"bureau": _make_output("bureau", "Score is 580")}
    report = await gs.compare(outputs, "test")
    assert isinstance(report, ReviewReport)
    assert len(report.resolved) == 0
    assert len(report.open_conflicts) == 0
