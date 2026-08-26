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
    """Specialist's structured answer. Field descriptions are reflected
    into the OpenAI structured-output schema, so they directly shape how
    much the model emits per field. Every extra token = ~20ms of
    generation wall-clock — keep brevity targets aggressive.

    The ≤50-word `findings` budget is calibrated for the FETCH question that
    dominates traffic ("how many", "what's the total") — one number is the
    whole answer, and prose around it is pure latency. It is the wrong budget
    for the OTHER shape: a sub-question that hands the specialist several
    DIRECTIONS to investigate, or asks what CONTRADICTS a claim. There the
    per-direction verdicts ARE the answer, and at 50 words across three
    directions the model drops the negatives first — which is exactly the half
    a reviewer needs ("(b) does not hold" is a result, not filler). So both
    fields carry a conditional widening, triggered by the sub-question's own
    shape. Default stays tight; only the mandate shape pays the extra tokens.
    See `skills/workflow/data_query.md` § INVESTIGATION MANDATES and § 1.2
    EXCEPTION, which must stay in step with the budgets stated here."""

    domain: str = Field(description="Domain key (one word, e.g. 'spend_payments').")
    mode: str = Field(description='"report" or "chat". Pick one word.')
    findings: str = Field(
        description=(
            "The answer in ≤50 words (≤2 sentences). State the concrete "
            "number, fact, or conclusion. NO restating the question, NO "
            "'I observed', NO methodology preamble. Numbers > prose. "
            "EXCEPTION — when the sub-question gave you DIRECTIONS to "
            "investigate, or asked what CONTRADICTS a claim: ≤40 words PER "
            "direction (~150 words total, ~200 max), one line each, and "
            "state the verdict for EVERY direction — including the ones the "
            "data did not support. A checked non-finding is a result; "
            "dropping it to save words is the failure mode. Still no "
            "preamble, still numbers > prose."
        ),
    )
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "≤3 bullets, ≤15 words each. Specific numbers / dates / "
            "row counts that back the finding. Skip the list entirely if "
            "findings already contains the key data points. EXCEPTION — "
            "under the investigation / contradiction shape above: ≤1 bullet "
            "per direction (≤5), ≤25 words each, each naming the column or "
            "window that settled that direction."
        ),
    )
    data_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "≤1 bullet, ≤15 words. Flag only a gap that materially affects "
            "THIS answer. Skip otherwise — do NOT enumerate generic "
            "data-quality concerns."
        ),
    )
    raw_data: dict = Field(
        default_factory=dict,
        description=(
            "DO NOT POPULATE. Leave as the empty dict. This field exists "
            "only for the rare audit case where raw rows are essential AND "
            "not already in evidence — for normal questions, omit content."
        ),
    )


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
    `_active_kps` filter (in agent_tool) keeps only the latest one.
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
    # Monotonic per-session turn counter (``CaseSession._qa_turn_seq``) stamped
    # at capture time. Unlike ``captured_at_turn`` — a random hex id that does
    # NOT sort — this orders KPs by age, which is what the working-set
    # compaction needs to protect the newest KPs from being dropped, and what
    # restores chronology after a relevance-ordered reload from Amem (see
    # ``memory/loader.py``). Missing (legacy KPs written before this field) is
    # treated as -1 = oldest, matching ``tools/episodic.py``'s convention.
    captured_at_seq: int | None = None
    confidence: Literal["high", "medium", "low"] = "medium"


class DistillerOutput(BaseModel):
    """Wrapper schema the distiller agent returns. Only `knowledge_points`
    is consumed; the wrapper exists so OpenAI's structured-output mode has
    a top-level object to enforce."""

    knowledge_points: list[KnowledgePoint] = Field(default_factory=list)


class ProposedOpportunity(BaseModel):
    """One actionable follow-up the synthesis proposes from the pins."""

    title: str = Field(description="Imperative, one line, e.g. 'Review RLI decline handling before a limit reduction'")
    rationale: str = Field(default="", description="Why these pins support it, in one or two sentences")


class PinSynthesis(BaseModel):
    """Synthesis over a reviewer's selected pins.

    Two modes share one schema so the UI renders one shape either way:
    `story` carries the narrative for "Reconstruct the story", and
    `opportunities` carries the proposals for "Propose opportunities". The
    unused half comes back empty rather than absent.
    """

    story: str = Field(default="", description="What the selected pins say when read together")
    opportunities: list[ProposedOpportunity] = Field(default_factory=list)
    # The honest half. A synthesis that only asserts is worse than useless to
    # a reviewer who has to defend it — this is where it says what the pins
    # do NOT settle.
    not_settled: list[str] = Field(
        default_factory=list,
        description="Questions these pins do not answer; what evidence is missing",
    )


class Resolution(BaseModel):
    pair: list[str]
    contradiction: str
    question_raised: str
    answer: str
    supporting_evidence: list[str] = Field(default_factory=list)
    conclusion: str
    # Re-answer mechanism: when general_specialist verifies a contradiction
    # and finds ONE specialist was wrong (canonical value matches the other,
    # or matches an authoritative aggregate the general specialist ran),
    # this field names the wrong specialist. The orchestrator's
    # post-general-specialist round reads this and re-invokes the named
    # specialist with the correction as part of the sub-question. Empty
    # / None when both specialists agreed or when the resolution doesn't
    # require a re-answer (e.g., paraphrasing differences).
    corrected_specialist: str | None = None
    # The canonical value general_specialist established (the date, count,
    # name, etc.) that the wrong specialist should adopt. Surfaced verbatim
    # in the re-invocation sub-question so the specialist can revise.
    # Nullable for parity with ``corrected_specialist``: when there's no
    # correction (the resolution is "complementary perspectives, no
    # contradiction"), the LLM naturally returns null for BOTH fields and
    # the schema must accept that. Previously this was a required ``str``
    # which raised ``Invalid JSON when parsing ... 1 validation error for
    # ReviewReport — resolved.0.corrected_value Input should be a valid
    # string`` and aborted the whole FinalAnswer, masking otherwise valid
    # specialist outputs.
    corrected_value: str | None = None


class Conflict(BaseModel):
    pair: list[str]
    contradiction: str
    question_raised: str
    reason_unresolved: str
    evidence_from_both: list[str] = Field(default_factory=list)


class ReviewDirective(BaseModel):
    """The reviewer's advisory output. The orchestrator ACTS on it; the
    reviewer never dispatches. See docs/superpowers/specs/
    2026-07-03-orchestrator-plan-review-dispatch-design.md."""
    kind: Literal["coherent", "needs_redispatch", "qualified_release"] = "coherent"
    # needs_redispatch:
    specialist: str | None = None          # who to re-run
    anchor: str | None = None              # window/event to anchor to, e.g. "2025-05"
    why: str | None = None                 # one line; feeds the sub-question + flags
    # qualified_release:
    release_specialist: str | None = None  # whose output is sufficient to ship


class ReviewReport(BaseModel):
    resolved: list[Resolution] = Field(default_factory=list)
    open_conflicts: list[Conflict] = Field(default_factory=list)
    cross_domain_insights: list[str] = Field(default_factory=list)
    data_requests_made: list[dict] = Field(default_factory=list)
    directive: ReviewDirective | None = None


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
    report_draft: ReportDraft | None = Field(
        default=None,
        description="DO NOT POPULATE — leave null. Provenance is extracted server-side.",
    )
    team_draft: TeamDraft | None = Field(
        default=None,
        description="DO NOT POPULATE — leave null. Provenance is extracted server-side.",
    )
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

    `named_variables` are real column names the question mentions, resolved
    against THIS case's data (`data_tools.known_variables_in`). Two uses, both
    of which failed before it existed: it lets a rejection be overridden when
    the reviewer names a live variable, and it tells the orchestrator that a
    bare token like "intoop" is a COLUMN — without it, "how is intoop" was
    answered as though INTOOP were the customer's name.

    `named_concepts` is the same idea one level up, and the distinction is
    load-bearing: `intoop` is a VARIABLE, `oop` is the pillar CONCEPT it sits
    under ("out-of-pattern: exposure vs. trailing-12-month remit"). A reviewer
    asks about either. Measured on consecutive turns, "how is intoop" passed
    while "how is oop" was REJECTED as off-topic — the variable lookup could
    not see it because oop is not a column name. Concepts are resolved against
    the ones THIS case's columns actually carry, so they override a rejection
    on the same footing as a variable.
    """

    passed: bool
    reason: str = ""
    redacted_question: str
    near_duplicate_of: str = ""
    near_duplicate_reason: str = ""
    named_variables: list[str] = Field(default_factory=list)
    named_concepts: list[str] = Field(default_factory=list)


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
