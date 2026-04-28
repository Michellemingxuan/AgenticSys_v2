import pytest
from models.types import (
    DomainSkill, SpecialistOutput, SynthesisResult, ReportSection,
    AnswerResult, DataRequestResult, ReviewReport, Resolution,
    Conflict, FinalOutput, DataGap, BlockedStep, LLMResult, StepRecord,
)


def test_domain_skill_creation():
    skill = DomainSkill(
        name="bureau",
        system_prompt="You are a bureau data expert.",
        data_hints=["bureau_full", "bureau_trades"],
        interpretation_guide="Focus on tradeline health and derogatory marks.",
        risk_signals=["90D+ delinquency", "score below 600"],
    )
    assert skill.name == "bureau"
    assert len(skill.data_hints) == 2


def test_specialist_output_creation():
    output = SpecialistOutput(
        domain="bureau",
        question="What is the delinquency trajectory?",
        mode="chat",
        findings="3 derog marks in last 12 months, score declining.",
        evidence=["bureau_full.derog_count = 3", "score dropped 680 → 620"],
        implications=["Delinquency risk is elevated and worsening."],
        data_gaps=[],
        raw_data={"bureau_full": [{"score": 620, "derog_count": 3}]},
    )
    assert output.domain == "bureau"
    assert len(output.evidence) == 2


def test_review_report_with_resolution():
    resolution = Resolution(
        pair=("bureau", "spend_payments"),
        contradiction="Bureau says low risk but payments show deterioration",
        question_raised="Is the bureau score lagging?",
        answer="Yes, score is 3 months stale.",
        supporting_evidence=["score_date = 2024-01-15", "3 missed payments since Feb"],
        conclusion="Payment behavior is the more current signal. Risk is higher than bureau suggests.",
    )
    report = ReviewReport(
        resolved=[resolution],
        open_conflicts=[],
        cross_domain_insights=["Bureau lag pattern detected — recommend score refresh."],
        data_requests_made=[{"intent": "bureau score timestamp"}],
    )
    assert len(report.resolved) == 1
    assert report.resolved[0].pair == ["bureau", "spend_payments"]


def test_final_output_with_data_gap():
    gap = DataGap(
        specialist="modeling",
        missing_data="model_scores table empty",
        absence_interpretation="No scoring run may indicate customer below scoring threshold.",
        is_signal=True,
    )
    output = FinalOutput(
        answer="Based on available evidence...",
        resolved_contradictions=[],
        open_conflicts=[],
        data_gaps=[gap],
        blocked_steps=[],
        specialists_consulted=["bureau", "modeling"],
    )
    assert output.data_gaps[0].is_signal is True
    assert "modeling" in output.specialists_consulted


def test_llm_result_success():
    result = LLMResult(status="success", data={"key": "value"}, error=None)
    assert result.status == "success"


def test_llm_result_blocked():
    result = LLMResult(status="blocked", data=None, error="Firewall rejection 403")
    assert result.status == "blocked"
    assert result.error is not None
