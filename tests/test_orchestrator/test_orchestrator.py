"""Tests for orchestrator.orchestrator (team planning + synthesis)."""

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
    TeamAssignment,
)
from orchestrator.orchestrator import Orchestrator


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


# ---- plan_team tests (team selection + sub-question decomposition) ----


def test_plan_team_parses_plan(firewall, logger):
    firewall.call = MagicMock(
        return_value=LLMResult(
            status="success",
            data={
                "plan": [
                    {"specialist": "bureau", "sub_question": "What is the current FICO?"},
                    {"specialist": "modeling", "sub_question": "What is the PD score?"},
                ]
            },
        )
    )

    registry = SessionRegistry()
    orch = Orchestrator(firewall, logger, registry, "credit_risk")
    plan = orch.plan_team(
        question="What is the credit risk?",
        available_specialists=["bureau", "modeling", "spend_payments", "wcc"],
        active_specialists=[],
    )

    assert len(plan) == 2
    assert all(isinstance(p, TeamAssignment) for p in plan)
    assert plan[0].specialist == "bureau"
    assert "FICO" in plan[0].sub_question
    assert plan[1].specialist == "modeling"


def test_plan_team_report_mode_returns_all_with_root_question(firewall, logger):
    # Report mode must not call the LLM — it picks every specialist with root question.
    firewall.call = MagicMock()
    registry = SessionRegistry()
    orch = Orchestrator(firewall, logger, registry, "credit_risk")
    available = ["bureau", "modeling"]

    plan = orch.plan_team(
        question="Full report please",
        available_specialists=available,
        active_specialists=[],
        mode="report",
    )

    firewall.call.assert_not_called()
    assert [p.specialist for p in plan] == available
    assert all(p.sub_question == "Full report please" for p in plan)


def test_plan_team_fallback_on_block(firewall, logger):
    firewall.call = MagicMock(
        return_value=LLMResult(status="blocked", error="denied")
    )
    registry = SessionRegistry()
    orch = Orchestrator(firewall, logger, registry, "credit_risk")
    available = ["bureau", "modeling"]

    plan = orch.plan_team(
        question="anything",
        available_specialists=available,
        active_specialists=[],
    )

    # Fallback: every available specialist, each with the root question verbatim.
    assert [p.specialist for p in plan] == available
    assert all(p.sub_question == "anything" for p in plan)


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
