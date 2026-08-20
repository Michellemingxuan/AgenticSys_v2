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
import time
import uuid

from agents import Runner

from tools.node_trace import (
    NodeTrace,
    NodeTraceRunHooks,
    _open_node,
    attach_io,
    attach_tag,
)

from llm.firewall_stack import redact_payload

# The reviewer's own fence. It used to borrow `ORCH_PLAN_TIMEOUT_S` (25s), and
# the two consumers want OPPOSITE things:
#
#   the round-1 watchdog  wants 25s TIGHT, because expiry there means "abandon
#                         this planning call and re-issue" -- it has a retry
#                         and disarms the deadline on the final attempt.
#   the reviewer          has NO retry. Expiry means the coherence gate is
#                         silently dropped for the turn, so it wants to
#                         OUTLAST a stall, not cut one short.
#
# At 25s it also sat under the per-call stall fence (40s), which made the
# call-layer retry unreachable: the phase fence always fired first. That is
# what killed `general_specialist` at exactly 25.00s in the private env while
# round_1 had run 0.00s.
#
# 120s holds one worst-case call -- 40s stall fence + 60s retry budget = 100 --
# with margin for the reviewer's own work. Dev is unaffected: 122 reviewer runs
# across the shipped logs, zero `review_failed`.
#
# Defined here rather than imported to avoid a server <-> review import cycle.
_REVIEWER_TIMEOUT_S = float(os.environ.get("REVIEWER_TIMEOUT_S", "120"))

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
    (``_REVIEWER_TIMEOUT_S`` — a single-LLM-call fence) and swallows every
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
        # Capture the reviewer run as a `general_specialist` node in the
        # node-trace, mirroring the orchestrator wrapping in conductor.py.
        # `_open_node` returns a null node when the store/scope is absent
        # (e.g. tests), so this stays a no-op there. The SDK RunHooks record
        # one child row per LLM round — the reviewer was previously invisible
        # in the node-trace because its Runner.run passed no hooks.
        store = getattr(ctx, "_node_trace_store", None)
        async with _open_node(store, "general_specialist", depth=0) as gs_nt:
            attach_tag("review")
            attach_io(messages_json=review_input)
            hooks = (
                NodeTraceRunHooks(store, gs_nt)
                if isinstance(gs_nt, NodeTrace) else None
            )
            result = await asyncio.wait_for(
                Runner.run(reviewer, review_input, context=ctx, hooks=hooks),
                timeout=_REVIEWER_TIMEOUT_S,
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


def _review_trace_payload(review, kind: str | None) -> dict:
    """Compact, PII-safe payload describing the coherence review for the
    reasoning-trace / orchestration-flow panels. ``review`` is the already-
    redacted ``ReviewReport`` (or ``None`` on timeout/error)."""
    if review is None:
        return {
            "verdict": "review_failed",
            "summary": "Coherence review did not complete (timeout or error).",
        }

    def _count(attr: str) -> int:
        try:
            return len(getattr(review, attr, None) or [])
        except Exception:  # noqa: BLE001
            return 0

    try:
        insights = [str(x) for x in
                    (getattr(review, "cross_domain_insights", None) or [])][:5]
    except Exception:  # noqa: BLE001
        insights = []

    payload: dict = {
        "verdict": kind or "coherent",
        "resolved": _count("resolved"),
        "open_conflicts": _count("open_conflicts"),
        "cross_domain_insights": insights,
    }
    directive = getattr(review, "directive", None)
    if directive is not None:
        payload["directive"] = {
            "kind": getattr(directive, "kind", None),
            "specialist": getattr(directive, "specialist", None),
            "why": getattr(directive, "why", None),
            "anchor": getattr(directive, "anchor", None),
        }
    return payload


def _emit_reviewer_trace(sess, turn_id, tool_calls, call_id, payload,
                         started_at: int) -> None:
    """Surface the coherence reviewer as a ``general_specialist`` node in the
    reasoning trace + orchestration-flow figure. Appends a synthetic tool-call
    record to ``tool_calls`` (which is what the QA cache stores and the
    cache-hit path replays, so the node survives cache replay too) and emits
    the matching ``agent_completed`` + refreshed ``team_plan``. Best-effort:
    a trace-emit failure must never wedge the turn."""
    try:
        duration_ms = max(0, int(time.time() * 1000) - started_at)
        tool_calls.append({
            "call_id": call_id,
            "tool": "general_specialist",
            "payload": payload,
            "duration_ms": duration_ms,
        })
        sess.emit("agent_completed", {
            "turn_id": turn_id, "call_id": call_id,
            "tool": "general_specialist",
            "payload": payload, "duration_ms": duration_ms,
        })
        sess.emit("team_plan", {"turn_id": turn_id,
                                "tool_calls": list(tool_calls)})
    except Exception:  # noqa: BLE001 — trace emission is never load-bearing
        pass


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
        # Surface the reviewer in the reasoning trace. `agent_started` fires
        # now (before the run); the matching `agent_completed` + `team_plan`
        # + tool_calls record are emitted below once the verdict is known.
        # `specialist_outputs` was built ABOVE from the pre-review tool_calls,
        # so appending our own `general_specialist` record can't feed the
        # reviewer its own output. (general_specialist is in every aux-tool
        # exclusion set, so it never counts toward the ≥2-specialist gate or
        # answer synthesis — it is trace-only.)
        gs_call_id = str(uuid.uuid4())
        gs_started_at = int(time.time() * 1000)
        try:
            sess.emit("agent_started", {
                "turn_id": turn_id, "call_id": gs_call_id,
                "tool": "general_specialist", "started_at": gs_started_at,
            })
        except Exception:  # noqa: BLE001 — trace emission is never load-bearing
            pass
        review = await _run_review(
            sess, ctx, framed_question, specialist_outputs
        )
        directive = getattr(review, "directive", None) if review else None
        kind = getattr(directive, "kind", None) if directive else None
        _emit_reviewer_trace(
            sess, turn_id, tool_calls, gs_call_id,
            _review_trace_payload(review, kind), gs_started_at,
        )
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
