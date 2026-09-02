# tests/test_server/test_plan_review_dispatch.py
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skills" / "workflow"


def test_team_construction_skill_content():
    body = (SKILLS / "team_construction.md").read_text(encoding="utf-8")
    # dispatch-shape guidance present
    assert "Dispatch shape" in body
    for shape in ("parallel", "collapse", "sequential"):
        assert shape in body.lower()
    # row-31 restriction removed
    assert "NOT TSR/CDSS" not in body


def test_orchestrator_instructions_vp_framing():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "agent_factories" / "orchestrator_agent.py").read_text(encoding="utf-8")
    assert "SINGLE response" not in src          # forced-parallel mandate removed
    assert "manager" in src.lower()              # VP framing present


# ── Task 6: server phased run — gating + dispatch-cap accessors ──────────────

@pytest.mark.asyncio
async def test_review_skipped_for_single_specialist(monkeypatch):
    import server
    from runner.turn import review
    calls = {"review": 0}

    async def fake_review(*a, **k):
        calls["review"] += 1
        return None

    monkeypatch.setattr(review, "_run_review", fake_review)
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments"}  # only 1
    assert review._is_multi_specialist_turn(ctx) is False


@pytest.mark.asyncio
async def test_review_runs_for_multi_specialist(monkeypatch):
    import server
    from runner.turn import review
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments", "modeling"}
    assert review._is_multi_specialist_turn(ctx) is True


def test_dispatch_cap_blocks_third_dispatch():
    import server
    from runner.turn import review
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._dispatch_count = 2
    assert review._dispatch_count(ctx) == 2
    # a re-dispatch request at count 2 must be refused by the caller (asserted
    # in the integration test); here we lock the accessor.


def test_dispatch_count_bump_clamps_at_two():
    import server
    from runner.turn import review
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._dispatch_count = 0
    assert review._dispatch_count(ctx) == 0
    assert review._bump_dispatch_count(ctx) == 1
    assert review._bump_dispatch_count(ctx) == 2
    # cap: never exceeds 2 dispatch rounds per turn
    assert review._bump_dispatch_count(ctx) == 2
    assert review._dispatch_count(ctx) == 2


def test_is_multi_specialist_turn_missing_attr_is_false():
    import server
    from runner.turn import review
    ctx = server.AppContext.__new__(server.AppContext)
    # no _domain_specialists_called attribute set at all
    assert review._is_multi_specialist_turn(ctx) is False


# ── Task 8: Integration — coherence-review → anchored re-dispatch ─────────────
#
# Trace scenario (case 366132845011): "did the spending spike, what drives it?"
# spend_payments returns a May-2025 spike; modeling returns stale 2024 drivers.
# The reviewer emits needs_redispatch(specialist="modeling", anchor="2025-05").
#
# Seam: _apply_review_directive(…, run_redispatch_pass_fn=…) is a module-level
# function extracted from the phased review block in server.py.  It is the
# smallest testable unit that exercises the complete decision path:
#   multi-specialist gate → _run_review → directive dispatch → cap check →
#   resume_input construction → run_redispatch_pass_fn call.
#
# _run_review is monkeypatched to return the directive; run_redispatch_pass_fn
# is injected as a fake that records what it was seeded with.
#
# Task 7 (early-release / straggler cancellation) is DEFERRED — not tested here.

@pytest.mark.asyncio
async def test_causal_question_redispatches_modeling_anchored(monkeypatch):
    """
    Assertions
    ----------
    (a) _dispatch_count ends at 2 (initial dispatch round 1 + exactly one
        server-enforced re-dispatch).
    (b) The modeling re-invocation is seeded with anchor "2025-05" in the
        injected resume_input (the [REVIEW DIRECTIVE] user turn).
    (c) The re-dispatched FinalAnswer replaces the stale phase-1 answer
        (returned new_final carries the 2025-anchored narrative, not the
        bare 2024 drivers).
    """
    import server
    from runner.turn import review
    from models.types import ReviewReport, ReviewDirective, FinalAnswer

    # ── Minimal fake session (logger + silent emit) ───────────────────────────
    class _Logger:
        def log(self, event, payload=None):
            pass  # swallow — we don't assert on log events in this test

    class _Session:
        logger = _Logger()
        case_id = "366132845011"

        def emit(self, event, payload):
            pass  # swallow SSE events

    sess = _Session()

    # ── ctx: 2 domain specialists already called → multi-specialist gate opens;
    #         dispatch round 1 already consumed by the phase-1 run ─────────────
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments", "modeling"}
    ctx._dispatch_count = 1   # phase-1 counts as round 1
    ctx._specialist_errors = []

    # ── Phase-1 tool_calls (spend spike found; modeling anchored to 2024) ─────
    tool_calls = [
        {
            "call_id": "c1",
            "tool": "spend_payments",
            "sub_question": "Did spend spike in May 2025?",
            "payload": {"findings": "Spend spiked 40% in May 2025."},
        },
        {
            "call_id": "c2",
            "tool": "modeling",
            "sub_question": "What drives spend changes?",
            "payload": {"findings": "Risk score drivers include 2024 utilization levels."},
        },
    ]

    # ── Fake phase-1 streamed result ─────────────────────────────────────────
    phase1_transcript = [
        {"role": "user", "content": "did the spending spike, what drives it?"}
    ]
    phase1_final = FinalAnswer(
        answer="Spend spiked. Modelling: 2024 utilization (stale).", flags=[]
    )

    class _FakeStreamed:
        def to_input_list(self):
            return list(phase1_transcript)   # copy so the test can inspect later

        final_output = phase1_final

    fake_streamed = _FakeStreamed()
    framed_question = "did the spending spike, what drives it?"
    turn_id = "t-8-integration"

    # ── Monkeypatch 1: _run_review returns needs_redispatch ───────────────────
    async def _fake_review(sess_arg, ctx_arg, question, specialist_outputs):
        return ReviewReport(
            directive=ReviewDirective(
                kind="needs_redispatch",
                specialist="modeling",
                anchor="2025-05",
                why=(
                    "modeling answer is anchored to 2024 utilization levels, "
                    "not the May-2025 spike window"
                ),
            )
        )

    monkeypatch.setattr(review, "_run_review", _fake_review)

    # ── Fake run_redispatch_pass_fn: records resume_input + returns 2025 answer
    redispatch_calls: list = []

    async def _fake_redispatch(resume_input):
        redispatch_calls.append(resume_input)
        return FinalAnswer(
            answer=(
                "Spend spiked 40% in May 2025. "
                "Modeling (anchored May-2025): elevated utilization peak "
                "in May-2025 drove the risk-score shift."
            ),
            flags=[],
        )

    # ── Call the review decision helper directly ──────────────────────────────
    new_final, review_flags = await review._apply_review_directive(
        sess=sess,
        ctx=ctx,
        framed_question=framed_question,
        tool_calls=tool_calls,
        streamed=fake_streamed,
        turn_id=turn_id,
        run_redispatch_pass_fn=_fake_redispatch,
    )

    # ── (b) dispatch count ends at exactly 2 ─────────────────────────────────
    assert review._dispatch_count(ctx) == 2, (
        f"expected _dispatch_count=2 after one re-dispatch, "
        f"got {review._dispatch_count(ctx)}"
    )

    # ── (a) modeling re-invocation was seeded with anchor "2025-05" ──────────
    assert redispatch_calls, "_run_redispatch_pass_fn was never called"
    resume_input = redispatch_calls[0]
    # resume_input is a list of message dicts; the [REVIEW DIRECTIVE] turn must
    # carry the anchor.
    resume_str = " ".join(
        str(item.get("content", "")) if isinstance(item, dict) else str(item)
        for item in resume_input
    )
    assert "2025-05" in resume_str, (
        f"expected anchor '2025-05' in the injected [REVIEW DIRECTIVE] message, "
        f"got resume_input content: {resume_str[:800]}"
    )

    # ── (c) re-dispatched result replaces the stale phase-1 answer ───────────
    assert new_final is not None, (
        "expected _apply_review_directive to return a non-None new_final "
        "when re-dispatch succeeds"
    )
    assert "May-2025" in new_final.answer or "2025-05" in new_final.answer, (
        f"expected 2025-anchored content in re-dispatched answer, "
        f"got: {new_final.answer}"
    )
    # Sanity: the phase-1 stale answer is NOT what came back
    assert new_final.answer != phase1_final.answer, (
        "re-dispatched final_answer must not be the stale phase-1 answer"
    )


@pytest.mark.asyncio
async def test_invalidate_specialist_distillation_cancels_and_wipes():
    """Re-dispatch KP hygiene — GENERAL, keyed on the re-dispatched specialist +
    turn_id (NOT specific to any one question): cancel that specialist's in-flight
    distill/autochart tasks and drop its this-turn KPs; leave OTHER specialists
    and PRIOR turns untouched."""
    import asyncio
    import types
    import server
    from runner.turn import review

    turn = "turn-abc"

    async def _long():
        await asyncio.sleep(30)

    d_x = asyncio.create_task(_long(), name="distill-modeling")
    a_x = asyncio.create_task(_long(), name="autochart-modeling")
    d_other = asyncio.create_task(_long(), name="distill-spend_payments")
    ctx = types.SimpleNamespace(
        _pending_distillers=[d_x, a_x, d_other],
        _specialist_kps={
            "modeling": [
                {"topic": "drivers_wrong_window", "captured_at_turn": turn},   # this-turn phase-1 (stale)
                {"topic": "prior_kp", "captured_at_turn": "older-turn"},       # prior turn (keep)
            ],
            "spend_payments": [
                {"topic": "spend_spike", "captured_at_turn": turn},            # other specialist (keep)
            ],
        },
    )

    stats = await review._invalidate_specialist_distillation(ctx, "modeling", turn)

    assert stats["tasks_cancelled"] == 2
    assert d_x.cancelled() and a_x.cancelled()
    assert ctx._pending_distillers == [d_other]          # other specialist's task kept
    assert not d_other.done()
    assert stats["kps_removed"] == 1
    assert [kp["topic"] for kp in ctx._specialist_kps["modeling"]] == ["prior_kp"]
    assert [kp["topic"] for kp in ctx._specialist_kps["spend_payments"]] == ["spend_spike"]

    d_other.cancel()
    try:
        await d_other
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_invalidate_specialist_distillation_tolerates_missing_state():
    """No pending list / no KP entry → graceful no-op for any specialist."""
    import types
    import server
    from runner.turn import review

    ctx = types.SimpleNamespace()  # no _pending_distillers, no _specialist_kps
    stats = await review._invalidate_specialist_distillation(ctx, "anything", "t")
    assert stats == {"tasks_cancelled": 0, "kps_removed": 0}


def test_team_construction_causal_dispatch_rule():
    """Prompt encourages sequencing/collapsing causal ('what drives X') questions
    so the review rarely has to repair a naive parallel dispatch."""
    body = (SKILLS / "team_construction.md").read_text(encoding="utf-8")
    assert "Causal questions are dependent" in body
    assert "what drives" in body.lower()
