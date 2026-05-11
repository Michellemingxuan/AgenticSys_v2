from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class DomainSkill(BaseModel):
    name: str
    system_prompt: str
    description: str = ""  # one-line summary from skill frontmatter; used to enrich orchestrator tool descriptions for routing
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


class KnowledgePoint(BaseModel):
    """One distilled, persistent unit of specialist knowledge that survives
    across turns within a case session.

    Produced by a second-pass `distiller` agent after each specialist run
    (see `agent_factories/distiller_agent.py`). Stored in
    `CaseSession.specialist_kb[<specialist_name>]`. Read back into the
    specialist's sub-question on subsequent calls so the specialist can
    answer follow-ups without re-running the same `summarize_trend` /
    `aggregate_column` queries.

    Supersession: when a new KP arrives with the same `topic`, the
    `_active_kps` filter (in redacting_tool) keeps only the latest one.
    Older entries are RETAINED in the KB list as an audit trail — never
    mutated, never deleted — so the case logger can reconstruct what was
    believed at any point in the session.

    `numbers` and `viz` together let a renderer reproduce a plot
    (matplotlib / Vega-Lite spec) without re-running the original tool
    call. Keep `numbers` small — series-shaped data, not raw row dumps.
    """

    topic: str
    claim: str
    numbers: list[dict] = Field(default_factory=list)
    viz: dict | None = None
    source_call: str = ""
    # Turn ids are short hex strings (e.g. ``uuid.uuid4().hex[:12]``) — NOT
    # ints. Typing as ``str | None`` avoids Pydantic coercion / validation
    # surprises when KPs round-trip through ``model_dump`` / ``model_validate``.
    captured_at_turn: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"


class DistillerOutput(BaseModel):
    """Wrapper schema the distiller agent returns. Only `knowledge_points`
    is consumed; the wrapper exists so OpenAI's structured-output mode has
    a top-level object to enforce."""

    knowledge_points: list[KnowledgePoint] = Field(default_factory=list)


class Resolution(BaseModel):
    pair: list[str]
    contradiction: str
    question_raised: str
    answer: str
    supporting_evidence: list[str] = Field(default_factory=list)
    conclusion: str


class Conflict(BaseModel):
    pair: list[str]
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


# `FinalOutput` is kept as a backwards-compat alias — callers have been
# migrated to `TeamDraft` (defined below), which is the canonical shape
# for the team-workflow branch. Remove this alias once all external
# consumers are updated.
FinalOutput = None  # placeholder; rebound after TeamDraft is defined.


class LLMResult(BaseModel):
    status: str  # "success" or "blocked"
    data: dict | None = None
    error: str | None = None


class StepRecord(BaseModel):
    prompt: str
    message: str
    result: dict | None = None
    attempt: int = 0


# ── New types for the Reports path (Phase 3) ─────────────────────────
#
# TeamDraft = what the team-workflow branch produces (today's FinalOutput shape
# will migrate onto this name in Phase 4; for now TeamDraft is a fresh alias
# that agents emit when the parallel orchestrator lands).
#
# ReportDraft = what the Report Agent produces after scanning curated
# case-folder reports.
#
# FinalAnswer = what the Balancing skill returns — merges both drafts.


class ReportDraft(BaseModel):
    """Result of the Report Agent's Needle + Analysis chain over `reports/<case-id>/`.

    `coverage` is the Needle's verdict on how the curated reports relate to
    the reviewer's question:
      - "explicit":      the report directly states the answer (or the
                         specific facts the question asks for).
      - "implicit":      the report contains relevant facts but does NOT
                         directly answer; inference / synthesis required.
                         Lead with specialist data; use the report as
                         supporting context.
      - "not_mentioned": the report doesn't cover the question's topic
                         (empty folder, irrelevant files, or no useful
                         content).

    The richer taxonomy replaces the older "full / partial / none" set,
    which biased the Needle toward over-confidently claiming "full" when
    a report merely touched on the topic.
    """

    coverage: Literal["explicit", "implicit", "not_mentioned"]
    answer: str = ""
    evidence_excerpts: list[str] = Field(default_factory=list)
    files_consulted: list[str] = Field(default_factory=list)


class TeamDraft(BaseModel):
    """The team-workflow branch's intermediate answer — same shape as today's
    `FinalOutput`, renamed for clarity in the parallel pipeline.

    Phase 4 wires the orchestrator to emit `TeamDraft` instead of `FinalOutput`
    for the team branch; `FinalOutput` stays as today's chat/report return type
    until that cut-over lands.
    """

    answer: str
    data_gap_summary: str = ""
    resolved_contradictions: list[Resolution] = Field(default_factory=list)
    open_conflicts: list[Conflict] = Field(default_factory=list)
    cross_domain_insights: list[str] = Field(default_factory=list)
    data_requests_made: list[dict] = Field(default_factory=list)
    data_gaps: list[DataGap] = Field(default_factory=list)
    blocked_steps: list[BlockedStep] = Field(default_factory=list)
    specialists_consulted: list[str] = Field(default_factory=list)
    sub_questions: list[TeamAssignment] = Field(default_factory=list)


class DataPullRequest(BaseModel):
    """Advisory signal emitted by the Balance step when specialist `data_gaps`
    and report `coverage` together suggest the answer is materially incomplete.

    No live pull backend exists today — this documents what a future Data Agent
    would target. Rendered to the reviewer by `ChatAgent.format_final_answer`.

    All fields except ``needed`` are optional so the LLM can emit
    ``{"needed": false}`` (or omit the object entirely) when no pull is
    warranted, without tripping the SDK's strict-JSON validation. When
    ``needed`` is true the LLM should populate ``reason`` and ``severity``;
    if it forgets, we still accept the response and downstream rendering
    falls back to the empty/low defaults.
    """

    needed: bool
    reason: str = ""
    would_pull: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "low"


class FinalAnswer(BaseModel):
    """Top-level reviewer-facing answer — the Balancing skill's output.

    Carries both drafts so the reviewer / downstream formatters can see
    provenance: which claim came from the curated report, which from the team
    workflow, and which discrepancies the Balancing skill flagged.

    `timeline` holds per-stage wall-clock timestamps — recorded by
    `Orchestrator.run` as a cheap forward-looking hook. Each entry is a dict
    with keys `{stage, started_at (ISO8601), ended_at (ISO8601),
    duration_ms (float)}`. Stages are `report_agent`, `team_workflow`, and
    `synthesis`. The first two run in parallel via `asyncio.gather`, so their
    time ranges overlap; `synthesis` starts after both branches complete.

    `data_pull_request` is set when the synthesis step judges the combined
    drafts insufficient to answer with confidence. Advisory only — no live
    pull backend is wired today.
    """

    answer: str
    flags: list[str] = Field(default_factory=list)
    report_draft: ReportDraft | None = None
    team_draft: TeamDraft | None = None
    timeline: list[dict] = Field(default_factory=list)
    data_pull_request: DataPullRequest | None = None


# Backwards-compat alias — see the placeholder earlier in this file.
FinalOutput = TeamDraft


class ScreenVerdict(BaseModel):
    """Output of ChatAgent.screen() — whether a reviewer's question is in-scope
    for case review, and what the redacted version of the question looks like
    after the redact skill ran.

    `passed=False` short-circuits the Orchestrator; `reason` is the
    reviewer-facing message explaining why.

    `near_duplicate_of` is the verbatim text of an earlier reviewer question
    in the same session that the relevance_check skill judged this question
    a near-duplicate of (matched on subject + time-range + scope). When set,
    the server replays that prior question's cached answer instead of
    re-running the orchestrator. Empty string when no match.
    `near_duplicate_reason` is a one-sentence justification for diagnostics.
    """

    passed: bool
    reason: str = ""
    redacted_question: str
    near_duplicate_of: str = ""
    near_duplicate_reason: str = ""


# Backwards-compat alias — `GuardrailVerdict` was the old name when input
# screening lived in a separate `GuardrailAgent`. Removed in a follow-up
# after external consumers migrate.
GuardrailVerdict = ScreenVerdict


class ClarifyResult(BaseModel):
    """Output of ChatAgent.clarify_intent() — decides whether the in-scope
    question needs clarification before dispatch to the orchestrator.

    Two-mode contract:
      - ``needs_clarification=False`` — question is unambiguous; ``options``
        is empty; the original question is dispatched to the orchestrator
        unchanged.
      - ``needs_clarification=True`` — surface ``options`` (2–4 reformulated
        candidate questions, each unambiguous) for the reviewer to pick from
        before the orchestrator is triggered. ``reason`` is a one-sentence
        explanation of why clarification is needed.
    """

    needs_clarification: bool
    options: list[str] = Field(default_factory=list)
    reason: str = ""
