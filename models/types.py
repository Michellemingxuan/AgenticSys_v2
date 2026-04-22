from __future__ import annotations
from pydantic import BaseModel, Field


class DomainSkill(BaseModel):
    name: str
    system_prompt: str
    data_hints: list[str] = Field(default_factory=list)
    interpretation_guide: str = ""
    risk_signals: list[str] = Field(default_factory=list)


class DataRequestResult(BaseModel):
    intent: str
    variables: list[str] = Field(default_factory=list)
    table_hints: list[str] = Field(default_factory=list)
    data: dict | None = None
    unavailable: bool = False
    unavailable_reason: str = ""


class SynthesisResult(BaseModel):
    question: str
    findings: str
    evidence: list[str] = Field(default_factory=list)
    implications: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    domain: str
    title: str
    key_findings: str
    supporting_evidence: list[str] = Field(default_factory=list)
    risk_implication: str = ""


class AnswerResult(BaseModel):
    domain: str
    question: str
    answer: str
    evidence: list[str] = Field(default_factory=list)


class SpecialistOutput(BaseModel):
    domain: str
    question: str
    mode: str  # "report" or "chat"
    findings: str
    evidence: list[str] = Field(default_factory=list)
    implications: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    raw_data: dict = Field(default_factory=dict)


class Resolution(BaseModel):
    pair: tuple[str, str]
    contradiction: str
    question_raised: str
    answer: str
    supporting_evidence: list[str] = Field(default_factory=list)
    conclusion: str


class Conflict(BaseModel):
    pair: tuple[str, str]
    contradiction: str
    question_raised: str
    reason_unresolved: str
    evidence_from_both: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    resolved: list[Resolution] = Field(default_factory=list)
    open_conflicts: list[Conflict] = Field(default_factory=list)
    cross_domain_insights: list[str] = Field(default_factory=list)
    data_requests_made: list[dict] = Field(default_factory=list)


class DataGap(BaseModel):
    specialist: str
    missing_data: str
    absence_interpretation: str
    is_signal: bool


class BlockedStep(BaseModel):
    specialist: str
    step: str
    error: str
    attempts: int


class TeamAssignment(BaseModel):
    """One specialist's slot in the orchestrator's team plan: the specialist
    picked for a question, and the tailored sub-question that specialist
    should answer. For atomic questions with a single specialist, sub_question
    equals the root question."""

    specialist: str
    sub_question: str


class FinalOutput(BaseModel):
    answer: str
    data_gap_summary: str = ""   # one concise summary of missing data across specialists
    resolved_contradictions: list[Resolution] = Field(default_factory=list)
    open_conflicts: list[Conflict] = Field(default_factory=list)
    cross_domain_insights: list[str] = Field(default_factory=list)
    data_requests_made: list[dict] = Field(default_factory=list)
    data_gaps: list[DataGap] = Field(default_factory=list)
    blocked_steps: list[BlockedStep] = Field(default_factory=list)
    specialists_consulted: list[str] = Field(default_factory=list)
    sub_questions: list[TeamAssignment] = Field(default_factory=list)


class LLMResult(BaseModel):
    status: str  # "success" or "blocked"
    data: dict | None = None
    error: str | None = None


class StepRecord(BaseModel):
    prompt: str
    message: str
    result: dict | None = None
    attempt: int = 0
