import pytest
from models.types import (
    Evidence,
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
        mode="chat",
        findings="3 derog marks in last 12 months, score declining.",
        evidence=[
            Evidence(claim="derogatory marks on file", value="3",
                     scope="bureau_full, all rows for the case"),
            Evidence(claim="FICO fell", value="680 → 620",
                     scope="bureau_full, 2024-01..2024-06"),
        ],
        data_gaps=[],
        raw_data={"bureau_full": [{"score": 620, "derog_count": 3}]},
    )
    assert output.domain == "bureau"
    assert len(output.evidence) == 2
    # Scope is the point of the structure — it must survive validation.
    assert output.evidence[0].scope.startswith("bureau_full")


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
    # The re-answer routing fields default to "unset" — orchestrator
    # treats this Resolution as "no re-invocation needed." Both fields
    # are nullable; defaulting to None mirrors how the LLM emits the
    # JSON for "no contradiction" cases (null, null) instead of forcing
    # the model to pick a sentinel string.
    assert report.resolved[0].corrected_specialist is None
    assert report.resolved[0].corrected_value is None


def test_resolution_accepts_null_corrected_value_from_json():
    """Regression for case 366132845011 — general_specialist returned a
    Resolution with `"corrected_value": null` (matching null
    corrected_specialist for a complementary-perspectives finding) and
    the previous `corrected_value: str = ""` schema rejected the whole
    ReviewReport with `Input should be a valid string`, masking the
    rest of the orchestrator's output. Both nullable now."""
    payload = {
        "pair": ["spend_payments", "modeling"],
        "contradiction": "Different framings, not a true contradiction",
        "question_raised": "Are these views reconcilable?",
        "answer": "Yes — complementary perspectives, no conflict.",
        "supporting_evidence": ["spend_payments: ...", "modeling: ..."],
        "conclusion": "Complementary perspectives.",
        "corrected_specialist": None,
        "corrected_value": None,
    }
    resolution = Resolution.model_validate(payload)
    assert resolution.corrected_specialist is None
    assert resolution.corrected_value is None

    # End-to-end via ReviewReport — this is the path the SDK takes when
    # it parses the orchestrator's FinalAnswer JSON.
    report = ReviewReport.model_validate({"resolved": [payload]})
    assert len(report.resolved) == 1
    assert report.resolved[0].corrected_value is None


def test_resolution_with_corrected_specialist_triggers_reanswer_routing():
    """When general_specialist verifies the canonical value and finds ONE
    specialist was wrong (e.g. on a date), it populates corrected_specialist
    + corrected_value. The orchestrator reads these to re-invoke the wrong
    specialist with the correction (Round 2.5 of the multi-specialist
    protocol). Other Resolution fields stay as evidence/audit."""
    resolution = Resolution(
        pair=["bureau", "modeling"],
        contradiction="bureau placed default at 2024-12; modeling placed it at 2025-01",
        question_raised="What is the canonical default date for this case?",
        answer="2024-12 per aggregate_column on crossbu_cards.month filtered to account_status='90 DPB'",
        supporting_evidence=["aggregate_column returned '2024-12'"],
        conclusion="bureau had the correct date; modeling was off by one month (likely picked up score-action timing instead of default-event timing).",
        corrected_specialist="modeling",
        corrected_value="2024-12",
    )
    assert resolution.corrected_specialist == "modeling"
    assert resolution.corrected_value == "2024-12"


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


# ── Evidence carries scope all the way to the reviewer ──────────────────────
#
# The point of structuring `evidence`: the tools now report what they measured
# over, and that scope was being dropped at the first LLM boundary. A reviewer
# reading "top merchant 10.0%" cannot tell whether the denominator was the whole
# history or one month. A required field cannot be forgotten; a directive can.

def test_scope_is_required_not_optional():
    """If scope were optional the model would omit it under token pressure,
    which is exactly the failure this schema change exists to prevent."""
    with pytest.raises(Exception):
        Evidence(claim="top merchant share", value="10.0%")


def test_evidence_survives_a_model_dump_round_trip():
    from models.types import SpecialistOutput as SO

    out = SO(domain="spend_payments", mode="chat",
             findings="Top merchant is 10.0% of spend.",
             evidence=[Evidence(
                 claim="top merchant's share of spend", value="10.0%",
                 scope="spends_data, Merchant Name contains S BERTRAM, "
                       "base = all 8,888 rows")])
    dumped = out.model_dump()
    ev = dumped["evidence"][0]
    assert ev["value"] == "10.0%"
    assert "base = all 8,888 rows" in ev["scope"], \
        "the denominator must reach whatever renders the answer"


def test_two_shares_of_the_same_number_are_distinguishable():
    """The case that motivated this: same numerator, different base, both
    'correct'. Structured scope is what makes them tellable apart."""
    whole = Evidence(claim="share of all spend", value="10.0%",
                     scope="spends_data, base = all 8,888 rows")
    y2025 = Evidence(claim="share of 2025 spend", value="30.6%",
                     scope="spends_data, base = 2,787 rows where Date contains 2025")
    assert whole.value != y2025.value
    assert "all 8,888" in whole.scope and "2,787" in y2025.scope
