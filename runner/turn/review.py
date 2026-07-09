"""Layer 3 — server-enforced coherence-review gate on multi-specialist turns.

These implement the server-enforced coherence-review gate on MULTI-specialist
turns (design: docs/superpowers/specs/
2026-07-03-orchestrator-plan-review-dispatch-design.md; SDK mechanic:
2026-07-03-phased-run-spike.md). The signatures are fixed — Task 7's
early-release path reuses them verbatim.

Extracted verbatim from ``server.py`` (Task 3 of the turn-runner-pipeline
refactor). The state-coupled ``_run_redispatch_pass`` closure stays in
``server.py`` and is passed into ``_apply_review_directive`` as
``run_redispatch_pass_fn`` — it is not part of this module.
"""
from __future__ import annotations

import asyncio
import json
import os

from agents import Runner

from llm.firewall_stack import redact_payload

# Round-1 (team-planning) watchdog, reused here as the single-LLM-call fence
# around the reviewer's ``Runner.run``. Defined independently of server.py's
# copy (server.py:123) to avoid a server <-> review import cycle; both read
# the same env var so the two stay in sync.
_ORCH_PLAN_TIMEOUT_S = float(os.environ.get("ORCH_PLAN_TIMEOUT_S", "25"))

# Aux tools that are NOT domain specialists (they never count toward the
# multi-specialist gate or the review's specialist_outputs).
_AUX_REVIEW_TOOLS = {"report_agent", "general_specialist"}


def _is_multi_specialist_turn(ctx) -> bool:
    """True iff ≥ 2 DISTINCT domain specialists were dispatched this turn.

    Reads ``ctx._domain_specialists_called`` (a set the agent_tool wrapper
    populates as each domain specialist runs). The server-enforced review +
    re-dispatch machinery only engages on multi-specialist turns; single-
    specialist turns keep the current single-run path (zero added latency).
    Defensive against a partially-constructed ctx (missing attribute → False).
    """
    called = getattr(ctx, "_domain_specialists_called", None) or set()
    return len(called) >= 2


def _dispatch_count(ctx) -> int:
    """Return the number of dispatch rounds driven this turn (accessor)."""
    return int(getattr(ctx, "_dispatch_count", 0) or 0)


def _bump_dispatch_count(ctx) -> int:
    """Increment the dispatch-round counter, clamped at the ≤ 2 cap.

    The design allows at most 2 dispatch rounds per turn (initial dispatch +
    ONE server-enforced re-dispatch). This never returns > 2; callers still
    guard on ``_dispatch_count(ctx) < 2`` BEFORE requesting a re-dispatch, but
    the clamp here is a defensive backstop so the cap can't be exceeded even
    on a double-call.
    """
    ctx._dispatch_count = min(_dispatch_count(ctx) + 1, 2)
    return ctx._dispatch_count


async def _run_review(sess, ctx, question: str, specialist_outputs: dict):
    """Invoke ``general_specialist`` (review-only) in SERVER code and return
    its ``ReviewReport``, or ``None`` on ANY failure.

    This is the guaranteed, un-skippable coherence gate on multi-specialist
    turns. It is wrapped in ``asyncio.wait_for`` on an existing timeout budget
    (``_ORCH_PLAN_TIMEOUT_S`` — a single-LLM-call fence) and swallows every
    exception (timeout included), logging ``review_failed``. The turn must
    NEVER block on the reviewer: a None return degrades gracefully to
    synthesis from whatever specialist outputs already exist (design §8).

    ``specialist_outputs`` maps specialist name → its (redacted) payload; it is
    serialized into the reviewer's input alongside the question so the reviewer
    judges coherence / anchoring and emits a ``ReviewDirective``.
    """
    try:
        from agent_factories.general_specialist import build_general_specialist
        reviewer = build_general_specialist(sess.clients.model)
        review_input = json.dumps(
            {"question": question, "specialist_outputs": specialist_outputs},
            default=str,
        )
        result = await asyncio.wait_for(
            Runner.run(reviewer, review_input, context=ctx),
            timeout=_ORCH_PLAN_TIMEOUT_S,
        )
        report = getattr(result, "final_output", None)
        try:
            report = redact_payload(report) if report is not None else None
        except Exception:  # noqa: BLE001 — redaction is best-effort here
            pass
        return report
    except Exception as exc:  # noqa: BLE001 — reviewer must never wedge a turn
        try:
            sess.logger.log("review_failed", {
                "exception_type": type(exc).__name__,
                "message": str(exc)[:300],
                "n_specialist_outputs": len(specialist_outputs or {}),
            })
        except Exception:  # noqa: BLE001
            pass
        return None


async def _invalidate_specialist_distillation(ctx, specialist: str, turn_id) -> dict:
    """Before re-dispatching ``specialist``, discard its now-superseded phase-1
    distillation for THIS turn so the corrected re-dispatch repopulates the KB
    cleanly (re-dispatch KB hygiene). Topic-supersession does NOT cover this: a
    mis-anchored phase-1 driver KP (e.g. ``..._2024_09``) has a different topic
    than the corrected one (``..._2025_05``), so both would otherwise stay active.

    Two steps, scoped strictly to ``specialist`` and this ``turn_id``:
      1. Cancel any in-flight ``distill-{specialist}`` / ``autochart-{specialist}``
         tasks in ``ctx._pending_distillers`` (a still-running phase-1 distiller
         must not persist a stale KP), and drop the specialist's tasks from the
         pending list.
      2. Remove ``ctx._specialist_kb[specialist]`` entries whose
         ``captured_at_turn == turn_id`` — this turn's phase-1 KPs (already
         persisted before we cancelled). Prior turns' KPs and OTHER specialists'
         KPs are untouched.
    Returns a small stats dict for logging.
    """
    stats = {"tasks_cancelled": 0, "kps_removed": 0}
    target_names = {f"distill-{specialist}", f"autochart-{specialist}"}

    pending = getattr(ctx, "_pending_distillers", None)
    if isinstance(pending, list):
        to_settle: list = []
        keep: list = []
        for t in pending:
            name = t.get_name() if hasattr(t, "get_name") else ""
            if name in target_names:
                if not t.done():
                    t.cancel()
                    to_settle.append(t)
                    stats["tasks_cancelled"] += 1
                # done or now-cancelled: drop from pending (KPs handled in step 2)
            else:
                keep.append(t)
        if to_settle:
            await asyncio.gather(*to_settle, return_exceptions=True)
        ctx._pending_distillers = keep

    kb = getattr(ctx, "_specialist_kb", None)
    if isinstance(kb, dict) and isinstance(kb.get(specialist), list):
        before = len(kb[specialist])
        kb[specialist] = [
            kp for kp in kb[specialist]
            if not (isinstance(kp, dict) and kp.get("captured_at_turn") == turn_id)
        ]
        stats["kps_removed"] = before - len(kb[specialist])

    return stats


async def _apply_review_directive(
    *,
    sess,
    ctx,
    framed_question: str,
    tool_calls: list,
    streamed,
    turn_id: str,
    run_redispatch_pass_fn,
) -> "tuple":
    """Server-enforced coherence review decision (Task 6 / §5.2).

    Calls ``_run_review``; on ``needs_redispatch`` (within the ≤2-round cap)
    bumps ``_dispatch_count``, injects a ``[REVIEW DIRECTIVE]`` user turn into
    the phase-1 transcript, and calls ``run_redispatch_pass_fn(resume_input)``
    for the phase-3 orchestrator re-run.

    Returns ``(new_final: FinalAnswer | None, review_flags: list[str])``.

    ``new_final`` is ``None`` when no re-dispatch occurred (coherent / capped /
    review failed). The caller is responsible for replacing ``final_answer``
    when ``new_final is not None``, and for calling ``_drain_specialist_errors``
    afterwards.

    Extracted from ``_run_turn_streamed`` to make the decision path testable
    without spinning up a live orchestrator: tests inject a fake
    ``run_redispatch_pass_fn`` and monkeypatch ``_run_review``.
    """
    review_flags: list[str] = []
    try:
        specialist_outputs = {
            c["tool"]: c.get("payload")
            for c in tool_calls
            if c.get("tool") not in _AUX_REVIEW_TOOLS and "payload" in c
        }
        review = await _run_review(
            sess, ctx, framed_question, specialist_outputs
        )
        directive = getattr(review, "directive", None) if review else None
        kind = getattr(directive, "kind", None) if directive else None
        try:
            sess.logger.log("review_done", {
                "turn_id": turn_id,
                "n_domain_specialists": len(
                    getattr(ctx, "_domain_specialists_called", set()) or set()
                ),
                "directive_kind": kind,
                "review_ran": review is not None,
            })
        except Exception:  # noqa: BLE001
            pass
        if kind == "needs_redispatch" and getattr(directive, "specialist", None):
            if _dispatch_count(ctx) < 2:
                _bump_dispatch_count(ctx)
                # Inject the directive as an appended user turn and resume
                # the orchestrator on the full phase-1 transcript (spike §4).
                _why = (directive.why
                        or "align the driver analysis to the event it explains.")
                _anchor = directive.anchor or "(the event window)"
                resume_input = streamed.to_input_list()
                resume_input.append({
                    "role": "user",
                    "content": (
                        f"[REVIEW DIRECTIVE] needs_redispatch: re-invoke "
                        f"`{directive.specialist}` anchored to {_anchor}. "
                        f"Reason: {_why} "
                        f"Re-run ONLY that specialist with the anchor folded "
                        f"into its sub-question, then synthesize the final "
                        f"answer."
                    ),
                })
                try:
                    sess.logger.log("review_redispatch", {
                        "turn_id": turn_id,
                        "specialist": directive.specialist,
                        "anchor": directive.anchor,
                        "dispatch_count": _dispatch_count(ctx),
                    })
                except Exception:  # noqa: BLE001
                    pass
                # Re-dispatch KB hygiene: discard the re-dispatched specialist's
                # now-superseded phase-1 distillation for this turn so only the
                # corrected (anchored) KPs survive. Best-effort — a hygiene
                # failure must not block the correction itself.
                try:
                    _inval = await _invalidate_specialist_distillation(
                        ctx, directive.specialist, turn_id)
                    sess.logger.log("review_redispatch_invalidated_distill", {
                        "turn_id": turn_id,
                        "specialist": directive.specialist,
                        **_inval,
                    })
                except Exception:  # noqa: BLE001 — best-effort KB hygiene
                    pass
                new_final = await run_redispatch_pass_fn(resume_input)
                review_flags.append(
                    f"coherence_review: re-dispatched "
                    f"`{directive.specialist}` anchored to "
                    f"{directive.anchor or 'the event window'} "
                    f"({directive.why or 'alignment fix'})."
                )
                return new_final, review_flags
            else:
                # Cap reached (design §6): synthesize with a residual flag.
                review_flags.append(
                    "coherence_review: re-dispatch needed but the ≤2 "
                    "dispatch-round cap was reached — the driver analysis "
                    "may not be fully anchored to the event window."
                )
                try:
                    sess.logger.log("review_capped", {
                        "turn_id": turn_id,
                        "specialist": getattr(directive, "specialist", None),
                        "dispatch_count": _dispatch_count(ctx),
                    })
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001 — never wedge the turn on review
        try:
            sess.logger.log("review_phase_error", {
                "turn_id": turn_id,
                "exception_type": type(exc).__name__,
                "message": str(exc)[:300],
            })
        except Exception:  # noqa: BLE001
            pass
    return None, review_flags
