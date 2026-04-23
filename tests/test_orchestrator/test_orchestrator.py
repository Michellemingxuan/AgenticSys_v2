"""Tests for orchestrator.orchestrator (team planning + synthesis)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.session_registry import SessionRegistry
from logger.event_logger import EventLogger
from models.types import (
    Conflict,
    FinalAnswer,
    FinalOutput,
    LLMResult,
    ReportDraft,
    ReviewReport,
    SpecialistOutput,
    TeamAssignment,
    TeamDraft,
)
from orchestrator.orchestrator import Orchestrator


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-orch", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


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


async def test_plan_team_parses_plan(mock_llm, logger):
    # plan_team now makes two sequential LLM calls:
    #   1. SELECT_TEAM → {"specialists": [...]}
    #   2. SPLIT_SUBQUESTIONS → {"plan": [...]}
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            LLMResult(status="success", data={"specialists": ["bureau", "modeling"]}),
            LLMResult(
                status="success",
                data={
                    "plan": [
                        {"specialist": "bureau", "sub_question": "What is the current FICO?"},
                        {"specialist": "modeling", "sub_question": "What is the PD score?"},
                    ]
                },
            ),
        ]
    )

    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")
    plan = await orch.plan_team(
        question="What is the credit risk?",
        available_specialists=["bureau", "modeling", "spend_payments", "wcc"],
        active_specialists=[],
    )

    assert len(plan) == 2
    assert all(isinstance(p, TeamAssignment) for p in plan)
    assert plan[0].specialist == "bureau"
    assert "FICO" in plan[0].sub_question
    assert plan[1].specialist == "modeling"
    # Two LLM calls: one per step.
    assert mock_llm.ainvoke.call_count == 2


async def test_plan_team_single_specialist_skips_split_call(mock_llm, logger):
    """When team-selection returns exactly one specialist, the sub-question
    decomposition step is skipped — sub-question equals the root verbatim,
    saving one LLM call."""
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(status="success", data={"specialists": ["bureau"]}),
    )

    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")
    root = "What is the current bureau score?"
    plan = await orch.plan_team(
        question=root,
        available_specialists=["bureau", "modeling"],
        active_specialists=[],
    )

    assert len(plan) == 1
    assert plan[0].specialist == "bureau"
    assert plan[0].sub_question == root
    # Only the selection call fired — decomposition short-circuited.
    assert mock_llm.ainvoke.call_count == 1


async def test_plan_team_report_mode_returns_all_with_root_question(mock_llm, logger):
    # Report mode must not call the LLM — it picks every specialist with root question.
    mock_llm.ainvoke = AsyncMock()
    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")
    available = ["bureau", "modeling"]

    plan = await orch.plan_team(
        question="Full report please",
        available_specialists=available,
        active_specialists=[],
        mode="report",
    )

    mock_llm.ainvoke.assert_not_called()
    assert [p.specialist for p in plan] == available
    assert all(p.sub_question == "Full report please" for p in plan)


async def test_plan_team_fallback_on_block(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(status="blocked", error="denied")
    )
    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")
    available = ["bureau", "modeling"]

    plan = await orch.plan_team(
        question="anything",
        available_specialists=available,
        active_specialists=[],
    )

    # Fallback: every available specialist, each with the root question verbatim.
    assert [p.specialist for p in plan] == available
    assert all(p.sub_question == "anything" for p in plan)


# ---- Orchestrator synthesize tests ----


async def test_synthesize_merges_outputs(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(
            status="success",
            data={
                "answer": "The credit risk is moderate.",
                "data_gap_assessments": [],
            },
        )
    )

    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")

    outputs = {"bureau": _make_output("bureau", "Score is 680")}
    report = ReviewReport()

    final = await orch.synthesize(outputs, report, "What is the credit risk?", "chat")

    assert isinstance(final, FinalOutput)
    assert final.answer == "The credit risk is moderate."
    assert "bureau" in final.specialists_consulted


async def test_synthesize_includes_open_conflicts(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(
            status="success",
            data={
                "answer": "Mixed signals on credit risk.",
                "data_gap_assessments": [],
            },
        )
    )

    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")

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

    final = await orch.synthesize(outputs, report, "What is the credit risk?", "chat")

    assert isinstance(final, FinalOutput)
    assert len(final.open_conflicts) == 1
    assert final.open_conflicts[0].contradiction == "Low score but low DTI"


async def test_synthesize_handles_data_gaps(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
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
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")

    outputs = {
        "bureau": _make_output("bureau", "Score is 700", data_gaps=["payment_history_2024"])
    }
    report = ReviewReport()

    final = await orch.synthesize(outputs, report, "What is the credit risk?", "chat")

    assert isinstance(final, FinalOutput)
    assert len(final.data_gaps) >= 1
    signal_gaps = [g for g in final.data_gaps if g.is_signal]
    assert len(signal_gaps) >= 1
    assert signal_gaps[0].specialist == "bureau"


# ---- Phase 4: Balancing + parallel run ----


def _report_draft(coverage="full", answer="report answer") -> ReportDraft:
    return ReportDraft(
        coverage=coverage,
        answer=answer,
        evidence_excerpts=['"report quote"'],
        files_consulted=["bureau.md"] if coverage != "none" else [],
    )


def _team_draft(answer="team answer") -> TeamDraft:
    return TeamDraft(
        answer=answer,
        specialists_consulted=["bureau"],
    )


async def test_balance_calls_llm_and_returns_final_answer(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(
            status="success",
            data={
                "answer": "Merged answer lead with report, team confirms.",
                "flags": ["team specialist evidence aligns with report"],
            },
        )
    )

    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")

    final = await orch.balance(
        question="What is the bureau status?",
        report_draft=_report_draft("full"),
        team_draft=_team_draft(),
    )

    assert isinstance(final, FinalAnswer)
    assert "Merged" in final.answer
    assert final.flags == ["team specialist evidence aligns with report"]
    assert final.report_draft.coverage == "full"
    assert final.team_draft.specialists_consulted == ["bureau"]
    assert mock_llm.ainvoke.call_count == 1


async def test_balance_falls_back_when_llm_blocked_coverage_none(mock_llm, logger):
    """Blocked LLM + coverage=none → deterministic 'team only' fallback with prefix note."""
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(status="blocked", error="denied"))
    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")

    final = await orch.balance(
        question="Q",
        report_draft=_report_draft(coverage="none", answer=""),
        team_draft=_team_draft(answer="team-only answer"),
    )

    assert isinstance(final, FinalAnswer)
    assert "No prior curated reports" in final.answer
    assert "team-only answer" in final.answer
    assert any("fallback" in f for f in final.flags)


async def test_balance_falls_back_when_llm_blocked_coverage_full(mock_llm, logger):
    """Blocked LLM + coverage=full → deterministic 'both sections' fallback."""
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(status="blocked", error="denied"))
    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")

    final = await orch.balance(
        question="Q",
        report_draft=_report_draft(coverage="full", answer="report says X"),
        team_draft=_team_draft(answer="team says Y"),
    )

    assert "report says X" in final.answer
    assert "team says Y" in final.answer
    assert any("fallback" in f for f in final.flags)


async def test_balance_falls_back_when_answer_empty(mock_llm, logger):
    """LLM returns success but empty answer → fallback fires (never ship a blank final)."""
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(status="success", data={"answer": "", "flags": []})
    )
    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")

    final = await orch.balance(
        question="Q",
        report_draft=_report_draft("full", answer="report answer"),
        team_draft=_team_draft("team answer"),
    )

    assert final.answer  # non-empty
    assert any("fallback" in f for f in final.flags)


async def test_run_dispatches_report_and_team_and_balances(mock_llm, logger, tmp_path):
    """End-to-end: orchestrator.run() dispatches both branches in parallel
    and merges via the Balancing skill."""
    # Mock LLM responses — order: plan_team.select, plan_team.split (skipped since 1 spec),
    # specialist.run 3 steps, general.compare (skipped, <2 specs), synthesize, balance.
    # Plus the report_agent's needle+analysis through the SAME mock_llm.
    # Easiest: mock via a side_effect sequence keyed on system_prompt keywords.

    def _ainvoke_router(system_prompt: str, user_message: str, **kwargs):
        sp = system_prompt.lower()
        if "report needle" in sp or "locate which files" in sp or "relevant_files" in sp:
            return LLMResult(
                status="success",
                data={
                    "relevant_files": ["bureau.md"],
                    "coverage": "partial",
                    "hints": ["bureau only"],
                },
            )
        if "report analyst" in sp or "evidence_excerpts" in sp:
            return LLMResult(
                status="success",
                data={
                    "answer": "Report says bureau FICO 620.",
                    "evidence_excerpts": ['"FICO score is 620"'],
                },
            )
        if "team selection" in sp or "pick the specialists" in sp:
            return LLMResult(status="success", data={"specialists": ["bureau"]})
        if "sub-question decomposition" in sp:
            return LLMResult(
                status="success",
                data={"plan": [{"specialist": "bureau", "sub_question": "What is FICO?"}]},
            )
        if "cross-domain reviewer" in sp:
            return LLMResult(
                status="success",
                data={"resolved": [], "open_conflicts": [], "cross_domain_insights": []},
            )
        if "orchestrator synthesizer" in sp or "data_gap_assessments" in sp:
            return LLMResult(
                status="success",
                data={
                    "answer": "Team says FICO is 620, moderate risk.",
                    "data_gap_assessments": [],
                },
            )
        if "balancing" in sp:
            return LLMResult(
                status="success",
                data={
                    "answer": "Report + team agree: FICO 620, moderate risk.",
                    "flags": [],
                },
            )
        # Base specialist 3-step chain uses the `data_query.md` body — distinct
        # enough not to collide with the above. Fall through to a generic response.
        return LLMResult(
            status="success",
            data={
                "findings": "FICO is 620.",
                "evidence": ["bureau_full row"],
                "implications": [],
                "data_gaps": [],
            },
        )

    mock_llm.ainvoke = AsyncMock(side_effect=_ainvoke_router)

    # Stage a case folder with one report file.
    case_folder = tmp_path / "CASE-TEST"
    case_folder.mkdir()
    (case_folder / "bureau.md").write_text("# Bureau\nFICO score is 620.")

    from agents.report_agent import ReportAgent
    registry = SessionRegistry()
    orch = Orchestrator(mock_llm, logger, registry, "credit_risk")
    report_agent = ReportAgent(mock_llm, logger)

    final = await orch.run("What is the bureau status?", case_folder, report_agent)

    assert isinstance(final, FinalAnswer)
    assert "FICO 620" in final.answer or "FICO is 620" in final.answer
    assert final.report_draft.coverage == "partial"
    assert final.report_draft.files_consulted == ["bureau.md"]
    assert final.team_draft.specialists_consulted == ["bureau"]
