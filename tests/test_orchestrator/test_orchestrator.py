"""Tests for orchestrator.team and orchestrator.orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.session_registry import SessionRegistry
from gateway.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import (
    Conflict,
    FinalOutput,
    LLMResult,
    ReviewReport,
    SpecialistOutput,
)
from orchestrator.orchestrator import Orchestrator
from orchestrator.team import TeamConstructor


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-orch", log_dir=str(tmp_path))


@pytest.fixture
def firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter, logger)


def _make_output(domain: str, findings: str, data_gaps=None) -> SpecialistOutput:
    return SpecialistOutput(
        domain=domain,
        question="test question",
        mode="chat",
        findings=findings,
        evidence=["ev1"],
        implications=["imp1"],
        data_gaps=data_gaps or [],
    )


# ---- TeamConstructor tests ----


def test_team_constructor_selects_specialists(firewall, logger):
    firewall.call = MagicMock(
        return_value=LLMResult(
            status="success",
            data={"specialists": ["bureau", "modeling"]},
        )
    )

    tc = TeamConstructor(firewall, logger)
    selected = tc.select_specialists(
        question="What is the credit risk?",
        pillar="credit_risk",
        available_specialists=["bureau", "modeling", "spend_payments", "wcc"],
        active_specialists=[],
    )

    assert selected == ["bureau", "modeling"]
    firewall.call.assert_called_once()


# ---- Orchestrator synthesize tests ----


def test_synthesize_merges_outputs(firewall, logger):
    firewall.call = MagicMock(
        return_value=LLMResult(
            status="success",
            data={
                "answer": "The credit risk is moderate.",
                "data_gap_assessments": [],
            },
        )
    )

    registry = SessionRegistry()
    orch = Orchestrator(firewall, logger, registry, "credit_risk")

    outputs = {"bureau": _make_output("bureau", "Score is 680")}
    report = ReviewReport()

    final = orch.synthesize(outputs, report, "What is the credit risk?", "chat")

    assert isinstance(final, FinalOutput)
    assert final.answer == "The credit risk is moderate."
    assert "bureau" in final.specialists_consulted


def test_synthesize_includes_open_conflicts(firewall, logger):
    firewall.call = MagicMock(
        return_value=LLMResult(
            status="success",
            data={
                "answer": "Mixed signals on credit risk.",
                "data_gap_assessments": [],
            },
        )
    )

    registry = SessionRegistry()
    orch = Orchestrator(firewall, logger, registry, "credit_risk")

    outputs = {
        "bureau": _make_output("bureau", "Score is 580"),
        "capacity_afford": _make_output("capacity_afford", "DTI is 25%"),
    }
    conflict = Conflict(
        pair=("bureau", "capacity_afford"),
        contradiction="Low score but low DTI",
        question_raised="Is the score stale?",
        reason_unresolved="No recent data",
        evidence_from_both=["score_580", "dti_25"],
    )
    report = ReviewReport(open_conflicts=[conflict])

    final = orch.synthesize(outputs, report, "What is the credit risk?", "chat")

    assert isinstance(final, FinalOutput)
    assert len(final.open_conflicts) == 1
    assert final.open_conflicts[0].contradiction == "Low score but low DTI"


def test_synthesize_handles_data_gaps(firewall, logger):
    firewall.call = MagicMock(
        return_value=LLMResult(
            status="success",
            data={
                "answer": "Risk assessment with gaps.",
                "data_gap_assessments": [
                    {
                        "specialist": "bureau",
                        "missing_data": "payment_history_2024",
                        "absence_interpretation": "May indicate no recent activity",
                        "is_signal": True,
                    }
                ],
            },
        )
    )

    registry = SessionRegistry()
    orch = Orchestrator(firewall, logger, registry, "credit_risk")

    outputs = {
        "bureau": _make_output("bureau", "Score is 700", data_gaps=["payment_history_2024"])
    }
    report = ReviewReport()

    final = orch.synthesize(outputs, report, "What is the credit risk?", "chat")

    assert isinstance(final, FinalOutput)
    assert len(final.data_gaps) >= 1
    signal_gaps = [g for g in final.data_gaps if g.is_signal]
    assert len(signal_gaps) >= 1
    assert signal_gaps[0].specialist == "bureau"
