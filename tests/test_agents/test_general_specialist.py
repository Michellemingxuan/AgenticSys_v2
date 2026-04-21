"""Tests for agents.general_specialist."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.general_specialist import GeneralSpecialist, COMPARE_SYSTEM_PROMPT
from gateway.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import LLMResult, ReviewReport, SpecialistOutput


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter, logger)


def _make_output(domain: str, findings: str) -> SpecialistOutput:
    return SpecialistOutput(
        domain=domain,
        question="test question",
        mode="chat",
        findings=findings,
        evidence=["ev1"],
        implications=["imp1"],
    )


def test_general_specialist_creation(firewall, logger):
    gs = GeneralSpecialist(firewall, logger)
    assert gs.firewall is firewall
    assert gs.logger is logger


def test_generate_pairs(firewall, logger):
    gs = GeneralSpecialist(firewall, logger)
    pairs = gs._generate_pairs(["bureau", "capacity"])
    assert len(pairs) == 1
    assert pairs[0] == ("bureau", "capacity")


def test_generate_pairs_three(firewall, logger):
    gs = GeneralSpecialist(firewall, logger)
    pairs = gs._generate_pairs(["bureau", "capacity", "spend"])
    assert len(pairs) == 3


def test_compare_returns_review_report(firewall, logger):
    firewall.call = MagicMock(
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

    gs = GeneralSpecialist(firewall, logger)
    outputs = {
        "bureau": _make_output("bureau", "Score is 580"),
        "capacity": _make_output("capacity", "DTI is 45%"),
    }
    report = gs.compare(outputs, "What is the credit risk?")

    assert isinstance(report, ReviewReport)
    assert len(report.resolved) == 1
    assert report.resolved[0].pair == ("bureau", "capacity")
    assert report.resolved[0].contradiction == "Score vs income mismatch"
    assert len(report.open_conflicts) == 0
    assert "Both domains flag high leverage" in report.cross_domain_insights


def test_compare_single_specialist(firewall, logger):
    gs = GeneralSpecialist(firewall, logger)
    outputs = {"bureau": _make_output("bureau", "Score is 580")}
    report = gs.compare(outputs, "test")
    assert isinstance(report, ReviewReport)
    assert len(report.resolved) == 0
    assert len(report.open_conflicts) == 0
