"""Unit tests for DataPullRequest and FinalAnswer.data_pull_request."""
from models.types import (
    DataPullRequest,
    FinalAnswer,
    ReportDraft,
    TeamDraft,
)


def _minimal_drafts():
    return (
        ReportDraft(coverage="none"),
        TeamDraft(answer="test"),
    )


def test_data_pull_request_basic():
    dpr = DataPullRequest(
        needed=True,
        reason="Missing bureau refresh",
        would_pull=["bureau.fico_latest"],
        severity="medium",
    )
    assert dpr.needed is True
    assert dpr.would_pull == ["bureau.fico_latest"]
    assert dpr.severity == "medium"


def test_final_answer_default_has_no_pull_request():
    report, team = _minimal_drafts()
    fa = FinalAnswer(answer="ok", report_draft=report, team_draft=team)
    assert fa.data_pull_request is None


def test_final_answer_with_pull_request():
    report, team = _minimal_drafts()
    dpr = DataPullRequest(
        needed=True, reason="x", would_pull=[], severity="low",
    )
    fa = FinalAnswer(
        answer="ok", report_draft=report, team_draft=team,
        data_pull_request=dpr,
    )
    assert fa.data_pull_request is not None
    assert fa.data_pull_request.severity == "low"
