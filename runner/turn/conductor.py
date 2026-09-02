"""The runtime spine for one reviewer turn — ``TurnRunner``.

This module holds the body of what used to be ``server.py::_run_turn_streamed``.
The turn is a sequence of phases (screen → cache-replay → assemble → run the
orchestrator → coherence review + re-dispatch → finalize); each phase is a
method and ``run()`` reads as the phase order. State the phases share lives on
``self`` — locals that used to cross phase boundaries in the monolithic
function are now attributes; the nested closures (``_emit_event``,
``_drain_specialist_errors``, ``_safe_dump``, ``_run_redispatch_pass``) are
methods sharing that state.

Mechanical rule for the extraction: state → ``self.``, closures → methods, no
logic edits. The body runs identically to the pre-refactor function.

SSE invariant: every code path that emits a ``final`` event also emits the full
replay set (``team_plan`` + ``agent_started`` + ``agent_completed`` + ``chart``)
— the cache-replay path (``_replay_from_cache``), the orchestrator-error
fallback, the empty-final salvage, and the success path all preserve this.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from agents import Runner
from agents.exceptions import AgentsException, ModelBehaviorError
from agents.items import ToolCallItem, ToolCallOutputItem

from models.app_context import AppContext
from llm.firewall_stack import redact_payload
from logger.process_timer import ProcessTimer
from memory import (
    ACTIVE_KP_KEEP,
    ACTIVE_KP_THRESHOLD,
    consolidate_agent_case,
    consolidate_case,
    kps_for_agent_turn,
    load_active_kps,
    load_case_kps,
    max_kp_seq,
    merge_recent_kps,
    load_case_summary,
    write_conversation,
    write_specialist_memory,
)
from models.types import FinalAnswer
from runner.config import (
    PILLAR,
    _AMEM_CONSOLIDATE_EVERY_N,
    _NODE_TRACE_STORE,
    _ORCH_PLAN_TIMEOUT_S,
    _PRIOR_QUESTIONS_FOR_SCREEN,
    _REPORTS_DIR,
    _SCREEN_STALL_RETRY_S,
    _SCREEN_TIMEOUT_S,
)
from runner.identity import SERVER_RUN_ID
from runner.orchestrator import Orchestrator
from runner.turn.cache import _find_kp, _get_cached_qa, _normalize_q, _store_cached_qa
from runner.turn.finalize import (
    _build_chart_payload, _collect_turn_charts, _replay_completed_specialists,
    _synthesize_fallback_answer,
)
from runner.turn.input_assembly import assemble_orchestrator_input
from runner.turn.review import (
    _apply_review_directive, _dispatch_count, _is_multi_specialist_turn,
)
from runner.turn.sse import map_run_item
from tools.episodic import EPISODIC_TURNS
from tools.fs_tools import is_report_file
from tools.node_trace import (
    NodeTrace, NodeTraceRunHooks, TURN_SCOPE, TurnScope,
    _open_node, attach_extra, attach_io, attach_latency, attach_tag,
    attach_usage,
)

# NOTE: `PILLAR`, `_SCREEN_TIMEOUT_S`, `_SCREEN_STALL_RETRY_S`,
# `_ORCH_PLAN_TIMEOUT_S`, `_REPORTS_DIR`,
# and `_NODE_TRACE_STORE` (imported from `runner.config` above) are plain
# module-level values, bound here by value at import time.
# `monkeypatch.setattr(server, "_SCREEN_TIMEOUT_S", ...)` (etc.) rebinds the
# name on the `server` module only — it does NOT reach the copies already
# bound into this module's namespace, so such patches will not affect the
# turn body below. Patch `runner.turn.conductor.<NAME>` directly instead.


async def _screen_with_stall_retry(sess, question: str,
                                   prior_questions: list[str]):
    """Run the screen phase, abandoning a STALLED attempt instead of waiting
    it out. Raises `asyncio.TimeoutError` if the whole phase budget is spent.

    Same bet as the safechain per-call fence (`_SAFECHAIN_STALL_RETRY_S`): a
    call still running long past the normal distribution is not working, it is
    wedged, and a fresh request has a fresh chance. Applied here because the
    per-call fence CANNOT help this phase — it is 40s against a 30s phase
    budget, so the phase fence always fires first and the reviewer just gets
    "question check took too long" with no second attempt.

    Sized against the phase's own target of <5s: 10s is well past a healthy
    screen, so a normal call never trips it. The two attempts SHARE the phase
    budget — the retry gets whatever is left of `_SCREEN_TIMEOUT_S` — so the
    worst case is unchanged and no caller has to be re-tuned.

    Re-running is safe: screening is a read-only classification, the coroutine
    is rebuilt from scratch, and the redact step self-skips for questions with
    no identifiers in them.
    """
    fence = min(_SCREEN_STALL_RETRY_S, _SCREEN_TIMEOUT_S)
    t0 = time.perf_counter()
    if fence > 0:
        try:
            return await asyncio.wait_for(
                sess.chat_agent.screen(question, prior_questions=prior_questions),
                timeout=fence,
            )
        except asyncio.TimeoutError:
            # Both channels: the JSONL for a human reading one session, the
            # trace tag for AgenticEval, which scores from the trace DB and
            # would otherwise never see that a stall happened at all.
            _tag_screen_node("stall_retry", screen_stalled_after_s=fence)
            sess.logger.log("screen_stalled", {
                "stalled_after_s": fence,
                "phase_budget_s": _SCREEN_TIMEOUT_S,
                "note": "abandoned the wedged screen call; re-issuing once",
            })

    remaining = _SCREEN_TIMEOUT_S - (time.perf_counter() - t0)
    if remaining <= 0:
        raise asyncio.TimeoutError()
    try:
        return await asyncio.wait_for(
            sess.chat_agent.screen(question, prior_questions=prior_questions),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        if fence > 0:
            # THE FALSIFYING SIGNAL. If this is rare the wedge belongs to the
            # individual request and the retry earns its place; if it is common
            # the stall is server-side and re-issuing only wastes the budget.
            _tag_screen_node("stall_retry_failed",
                             screen_retry_budget_s=round(remaining, 2))
            sess.logger.log("screen_retry_stalled", {
                "retry_budget_s": round(remaining, 2),
                "phase_budget_s": _SCREEN_TIMEOUT_S,
            })
        raise


def _tag_screen_node(tag: str, **extra) -> None:
    """Mark the open `screen` node. Never raises — telemetry must not be able
    to break the phase it describes."""
    try:
        attach_tag(tag)
        if extra:
            attach_extra(**extra)
    except Exception:  # noqa: BLE001
        pass


class _TurnAborted(Exception):
    """Raised from `_run_turn_streamed` checkpoints when the session's
    `cancel_in_flight` event is set (typically by `post_rewind`). The
    `_spawn_turn._runner` outer wrapper catches it, logs the abort,
    emits a `turn_done` with `outcome="aborted"`, and releases the
    lock so the next queued turn can proceed."""


# Injected to resume the orchestrator when it emitted FinalAnswer without ever
# calling report_agent (see `_ensure_report_agent`).
_REPORT_AGENT_DIRECTIVE = (
    "[REQUIRED report_agent] You emitted FinalAnswer without calling "
    "`report_agent`, which is MANDATORY on every turn — including pure "
    "extraction / list questions. Call `report_agent` now for this question, "
    "then re-emit FinalAnswer incorporating whatever it returns. If the curated "
    "reports do not cover this question, report_agent will say so ('not covered "
    "by the reports') — surface that briefly rather than omitting it."
)

# Injected on the no-tools retry: the orchestrator emitted a direct — and
# ungrounded — FinalAnswer on round 1 instead of dispatching. Re-running
# verbatim tends to repeat the mistake, so the retry escalates with a hard,
# format-specific directive.
#
# NOT a safechain limitation. This comment used to say `tool_choice="required"`
# was "only a text instruction" there; that was measured FALSE in the private
# env on 2026-07-29 (`tools/safechain_probe.py`) and the text protocol was
# deleted in b97375c — safechain is a real `AzureChatOpenAI` subclass and
# enforces `tool_choice` natively, same as the OpenAI path. So this is a
# model-behaviour backstop on BOTH transports, not compensation for a missing
# capability. See `.claude/memory/safechain_dual_environment.md`, whose open
# item is to retire this machinery once a prod run validates the removal.
_NO_TOOLS_RETRY_DIRECTIVE = (
    "[REQUIRED — you called ZERO specialists] Your previous response was a "
    "direct answer with NO tool call. That answer is UNGROUNDED and forbidden: "
    "you have not queried any live data. You MUST dispatch at least one domain "
    "specialist THIS round. Respond with ONLY a tool call — a single JSON object "
    '`{"tool_call": {"name": "<specialist_name>", "arguments": {"sub_question": '
    '"<what to investigate>"}}}` (or a `{"tool_calls": [ ... ]}` array for '
    "several). Do NOT write a prose answer, do NOT wrap it in ```markdown fences, "
    "and do NOT emit a FinalAnswer this round."
)


# Appended to the orchestrator input when a case has NO curated reports yet, so
# report_agent (normally MANDATORY) is neither expected nor enforced — a fresh
# case with no reports must answer smoothly from live specialist data alone.
# End-of-turn drain of the fire-and-forget distiller / auto-chart tasks. The
# answer text is already on screen by then, so this only bounds how long charts
# may still land — on expiry the turn completes and the un-drained KPs are in
# RAM but were never written to Amem.
# [config/tuning.yaml: timeouts.distiller_drain_s]
_DISTILLER_DRAIN_TIMEOUT_S = float(
    os.environ.get("DISTILLER_DRAIN_TIMEOUT_S", "60")
)


_NO_REPORTS_NOTE = (
    "[NOTE] This case has NO curated reports available yet. Do NOT call "
    "`report_agent` this turn — there is nothing to look up. Answer from the "
    "domain specialists' live data only, and prefix the answer with "
    "\"No prior curated reports — answer is from live specialist analysis only.\""
)


# The complement of the note above, and the reason it exists: report_agent
# returns `coverage="not_mentioned"` for BOTH "the folder is empty" and "no file
# was relevant to this question". Synthesis Path A keys off that one value, so a
# case WITH reports whose reports simply do not discuss the question was telling
# the reviewer that no reports exist — a falsifiable claim, and wrong. Only the
# server knows which is true, so it says so here.
_REPORTS_PRESENT_NOTE = (
    "[NOTE] This case HAS curated reports. If `report_agent` returns "
    "coverage=\"not_mentioned\", that means the reports do not address THIS "
    "question — it does NOT mean the case has no reports. In that case prefix "
    "the answer with \"Prior reports do not address this question — answer is "
    "from live specialist analysis only.\" Never state or imply that no curated "
    "reports exist for this case."
)



# Scope provenance lives in the REASONING TRACE, not the final answer.
#
# `agent_tool` attaches a one-line `scope` (`table: window` pairs) and per-call
# `measured_over` lines to each specialist's payload, so both already travel to
# every trace surface. The answer text stays clean: scope is provenance a
# reviewer opens the trace for, not part of the finding being reported.


def _cacheable_tool_calls(tool_calls: list, degraded_names) -> list[dict]:
    """The qa_cache projection of this turn's specialist calls.

    Keeps the fields a cache-hit replay needs to repopulate the
    orchestrator-flow / specialists panel, and stamps `degraded` for any
    specialist whose answer rested on a tool call that FAILED and never
    recovered (`agent_tool` registers those on `ctx._degraded_specialists`).

    The payload of a degraded call is still cached — a replay must look
    identical to the original turn — but `episodic.build_records` drops the
    flagged entries, which is what stops one turn's ungrounded answer from
    becoming the next turn's context.
    """
    names = set(degraded_names or ())
    return [
        {
            "call_id": tc.get("call_id"),
            "tool": tc.get("tool"),
            "sub_question": tc.get("sub_question"),
            "payload": tc.get("payload"),
            "duration_ms": tc.get("duration_ms"),
            "degraded": tc.get("tool") in names,
        }
        for tc in tool_calls
    ]


# What counts as a report lives in `tools/fs_tools.REPORT_SUFFIXES` — imported,
# not restated, so detection cannot drift from what report_agent is shown and
# from what it can actually read. A curated `.docx` or `.pdf` stays invisible
# to the whole path: a reader limitation, not a detection one.


def _case_has_reports(case_id: str) -> bool:
    """True iff the case has at least one curated report file to look up.

    A case with no reports has either no ``reports/<case_id>`` folder at all, or
    one holding only generated artifacts (e.g. a ``charts/`` subdir) and no
    report files. Such cases must not be hindered by the mandatory-report_agent
    machinery — there is simply nothing to consult.

    Recursive and case-insensitive on purpose. The old check counted only
    top-level, exactly-``.md`` files, so a report filed one directory down or
    saved as ``.MD``/``.txt`` read as "this case has no reports" — which both
    suppresses report_agent AND makes the answer assert to the reviewer that no
    reports exist. Charts still do not count: ``.png`` is not in the set.
    """
    folder = _REPORTS_DIR / case_id
    try:
        return folder.is_dir() and any(
            is_report_file(p) for p in folder.rglob("*")
        )
    except OSError:
        # A transient IO error (network/Drive-backed folders) must not be read
        # as "no reports" — that is a claim about the CASE, not about the disk.
        # Assume reports exist and let report_agent report what it finds.
        return True


class _PlanningTimeout(Exception):
    """Round-1 (team-planning) stalled past ORCH_PLAN_TIMEOUT_S — abort + retry."""


async def _next_planning_event(stream_iter, plan_deadline, first_tool_seen):
    """Await the next orchestrator stream event, bounded by the planning deadline.

    While the orchestrator is still PLANNING (no tool call emitted yet) and a
    ``plan_deadline`` (``time.monotonic`` seconds) is armed, bound the wait by
    the remaining budget and raise ``_PlanningTimeout`` if it elapses — so a
    stalled round-1 call is cancelled and retried with a fresh request instead
    of dead-waiting. Once the first tool call has landed (``first_tool_seen``)
    the deadline is disarmed and later events stream under the normal turn
    wall-clock fence. ``plan_deadline=None`` (the final attempt) also disarms it,
    so a genuinely slow-but-working env degrades to waiting, not hard failure.
    """
    if plan_deadline is not None and not first_tool_seen:
        budget = plan_deadline - time.monotonic()
        if budget <= 0:
            raise _PlanningTimeout()
        try:
            return await asyncio.wait_for(stream_iter.__anext__(), timeout=budget)
        except asyncio.TimeoutError as exc:
            raise _PlanningTimeout() from exc
    return await stream_iter.__anext__()


class TurnRunner:
    """The runtime spine for one reviewer turn. Each phase is a method;
    run() is the readable order. State the phases share lives on self."""

    def __init__(self, sess, turn_id, question, started_at=None):
        self.sess = sess
        self.turn_id = turn_id
        self.question = question
        if started_at is None:
            started_at = int(time.time() * 1000)
        self.started_at = started_at
        # Set the per-turn scope contextvar so `_open_node()` calls anywhere
        # downstream (chat_agent.screen, agent_tool, orchestrator) can build
        # a NodeTrace without passing chat/case/turn IDs through every call site.
        # `chat_id` is the deterministic conversation_id (restart-invisible
        # grouping axis); `server_run_id` is a per-process diagnostic value.
        # The `sess.logger.session_id` fallback keeps any non-server
        # construction path (e.g. a unit harness building a bare session)
        # from writing an empty chat_id.
        _conv = getattr(sess, "conversation_id", "") or sess.logger.session_id
        TURN_SCOPE.set(TurnScope(
            chat_id=_conv,
            case_id=sess.case_id,
            turn_id=turn_id,
            conversation_id=_conv,
            server_run_id=SERVER_RUN_ID,
            user_id=getattr(sess, "user_id", ""),
            pillar_id=getattr(sess, "pillar_id", ""),
        ))
        self.turn_timer = ProcessTimer(
            sess.logger,
            "turn",
            turn_id=turn_id,
            case_id=sess.case_id,
        )
        # Wall clock, not the timer's `perf_counter` start: this is stored in
        # the qa_cache and rendered as a clock time beside the question in a
        # restored thread, so it has to be an epoch a browser can format.
        self.turn_started_at = time.time()
        # ── filled by phases ──────────────────────────────────────────────
        self.verdict = None
        self.cache_key = None
        self.orchestrator = None
        self.ctx = None
        self.framed_question = None
        self.run_input = None
        self.streamed = None
        self.final_answer: FinalAnswer | None = None
        self.review_flags: list[str] = []
        self.has_reports = True  # set in _assemble_input from the case's reports
        # ── SSE-emit state (Phase 3) ──────────────────────────────────────
        self.call_index_by_id: dict[str, int] = {}  # call_id → index in tool_calls
        self.tool_calls: list[dict] = []
        self.started_at_by_call: dict[str, int] = {}
        self.team_plan_emitted = False
        self.first_tool_call_logged = False
        # Cursor over ctx._specialist_errors so we emit a typed `error` SSE
        # event exactly once per failure, as soon as the agent_tool wrapper
        # records it. Without this, the reviewer would only see a vague
        # `agent_completed` carrying a "[FAILED …]" string.
        self.specialist_errors_emitted = 0

    # ── shared helpers (were nested closures) ─────────────────────────────

    def _emit_event(self, event_name: str, payload: dict) -> None:
        """Emit hook for tools that want to publish typed SSE events DURING
        the run (not just at end-of-turn). Used today by `make_chart`. Stamps
        `turn_id` so tool callers don't have to. Guards against `sess.emit`
        raising (closed connection, etc.) so a streaming failure to one client
        never poisons the agent run for the rest of the session."""
        try:
            self.sess.emit(event_name, {**payload, "turn_id": self.turn_id})
        except Exception:  # noqa: BLE001
            pass

    def _drain_specialist_errors(self) -> None:
        errors = getattr(self.ctx, "_specialist_errors", None) or []
        while self.specialist_errors_emitted < len(errors):
            err = errors[self.specialist_errors_emitted]
            self.sess.emit("turn_error", {
                "turn_id": self.turn_id,
                "specialist": err.get("specialist"),
                "error_type": err.get("error_type"),
                # Raw human message ONLY. The frontend composes the display from
                # the separate `specialist` (node key) + `error_type` (prefix)
                # fields — embedding them here too produced a doubled label
                # ("timeout: report_agent: timeout: …").
                "message": err.get("error_message"),
                "sub_question": err.get("sub_question"),
                "recoverable": True,
            })
            self.specialist_errors_emitted += 1

    def _safe_dump(self, obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if isinstance(obj, dict):
            return {k: self._safe_dump(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._safe_dump(v) for v in obj]
        return obj

    # ── Phase 1: question check (screen + relevance) ──────────────────────

    async def _screen(self) -> bool:
        sess = self.sess
        turn_id = self.turn_id
        # Build the list of prior reviewer questions in this session so the
        # relevance_check skill can flag near-duplicates (matched on subject +
        # time-range + scope). The qa_cache holds raw redacted-question strings
        # as values' "origin_question"; we surface those here.
        timer_t0 = time.perf_counter()
        prior_questions = [v.get("origin_question", "") for v in sess.qa_cache.values()]
        prior_questions = [q for q in prior_questions if q]
        # Cap to the N most-recent prior questions to keep the relevance_check
        # prompt bounded as the qa_cache fills over the session. qa_cache uses
        # LRU ordering (insertion order + move-to-end on hits), so the last N
        # entries are the most recently used / asked. See _PRIOR_QUESTIONS_FOR_SCREEN.
        if len(prior_questions) > _PRIOR_QUESTIONS_FOR_SCREEN:
            prior_questions = prior_questions[-_PRIOR_QUESTIONS_FOR_SCREEN:]
        self.turn_timer.record(
            "prior_question_scan",
            int((time.perf_counter() - timer_t0) * 1000),
            prior_questions=len(prior_questions),
            qa_cache_entries=len(sess.qa_cache),
        )
        screen_t0 = time.time()
        try:
            # Wrap the screen phase in a node trace so it appears in the
            # trace DB alongside the orchestrator/specialist nodes. The
            # chat_agent creates child nodes (chat.redact, chat.relevance_check)
            # inside; this parent groups them under one "screen" row.
            async with _open_node(_NODE_TRACE_STORE, "screen", depth=0):
                verdict = await _screen_with_stall_retry(
                    sess, self.question, prior_questions,
                )
        except asyncio.TimeoutError:
            sess.logger.log("screen_timeout", {
                "turn_id": turn_id,
                "limit_s": _SCREEN_TIMEOUT_S,
                "prior_questions_sent": len(prior_questions),
            })
            self.turn_timer.summary(outcome="screen_timeout")
            sess.emit("turn_error", {
                "turn_id": turn_id,
                "message": (
                    f"Question-check phase exceeded the {_SCREEN_TIMEOUT_S:.0f}s "
                    f"budget — likely a transient LLM-backend stall. Try again; "
                    f"if it recurs, clearing case history via the rewind action "
                    f"can shrink the screen prompt and unstick it."
                ),
                "recoverable": False,
            })
            sess.emit("turn_done", {
                "turn_id": turn_id,
                "ended_at": int(time.time() * 1000),
                "duration_ms": int(time.time() * 1000) - self.started_at,
                "outcome": "screen_timeout",
            })
            return False
        except Exception as exc:
            self.turn_timer.summary(outcome="screen_failed")
            sess.emit("turn_error", {"turn_id": turn_id, "message": f"screen failed: {exc}", "recoverable": True})
            sess.emit("turn_done", {"turn_id": turn_id, "ended_at": int(time.time() * 1000),
                                    "duration_ms": int(time.time() * 1000) - self.started_at,
                                    "outcome": "orchestrator_error"})
            return False

        screen_duration_ms = int((time.time() - screen_t0) * 1000)
        sess.logger.log("turn_phase_screen_done", {
            "turn_id": turn_id,
            "duration_ms": screen_duration_ms,
            "passed": verdict.passed,
        })
        self.turn_timer.record("screen", screen_duration_ms, passed=verdict.passed)

        # Cooperative-cancellation checkpoint #1 (post-screen). If the user
        # rewound during the screen LLM call, the cancel_in_flight event was
        # set; abort cleanly here rather than continuing into the (often
        # much longer) orchestrator phase. See `_TurnAborted` docstring.
        if sess.cancel_in_flight.is_set():
            raise _TurnAborted("rewind during screen phase")

        in_scope = verdict.passed
        outcome_after_screen = "ok" if in_scope else "screen_rejected"
        sess.emit("question_check", {
            "turn_id": turn_id,
            "passed": verdict.passed,
            "reason": verdict.reason,
            "redacted_question": verdict.redacted_question,
            "in_scope": in_scope,
            "outcome": outcome_after_screen,
        })

        if not verdict.passed:
            # Treat reject as the final answer — emit synthesis + agent_message.
            rejection_text = f"[rejected] {verdict.reason}"
            ts = int(time.time() * 1000)
            sess.emit("final", {
                "turn_id": turn_id, "answer": rejection_text, "flags": [verdict.reason],
                "timeline": [], "data_pull_request": None,
            })
            sess.emit("agent_message", {
                "id": str(uuid.uuid4()), "role": "agent", "text": rejection_text,
                "timestamp": ts, "turn_id": turn_id,
            })
            sess.emit("turn_done", {"turn_id": turn_id, "ended_at": ts,
                                    "duration_ms": ts - self.started_at, "outcome": "screen_rejected"})
            self.turn_timer.summary(outcome="screen_rejected")
            return False

        self.verdict = verdict
        return True

    # ── Phase 1.5: cache lookup — exact-match first, then near-duplicate ───

    async def _replay_from_cache(self) -> bool:
        sess = self.sess
        turn_id = self.turn_id
        verdict = self.verdict
        # Cache key uses the redacted-question normalized form so identical
        # questions with different identifiers (case IDs etc.) collide as
        # intended. Rejections are not cached.
        timer_t0 = time.perf_counter()
        cache_key = _normalize_q(verdict.redacted_question)
        self.cache_key = cache_key
        cached = _get_cached_qa(sess, cache_key)
        cache_hit_kind = "exact" if cached is not None else None
        # Fall back to relevance_check's near-duplicate verdict — the LLM
        # judged this question a near-duplicate of an earlier one along
        # subject + time-range + scope dimensions. Look up that prior
        # question's cached answer.
        if cached is None and verdict.near_duplicate_of:
            near_dup_key = _normalize_q(verdict.near_duplicate_of)
            cached = _get_cached_qa(sess, near_dup_key)
            if cached is not None:
                cache_hit_kind = "near_duplicate"
                sess.logger.log("qa_cache_hit_near_duplicate", {
                    "turn_id": turn_id,
                    "matched_prior": verdict.near_duplicate_of,
                    "match_reason": verdict.near_duplicate_reason,
                })
        self.turn_timer.record(
            "qa_cache_lookup",
            int((time.perf_counter() - timer_t0) * 1000),
            hit=cached is not None,
            kind=cache_hit_kind,
        )
        if cached is not None:
            sess.logger.log("qa_cache_hit", {
                "turn_id": turn_id, "norm_q": cache_key,
                "origin_turn_id": cached.get("turn_id_origin"),
                "kind": cache_hit_kind,
            })
            # Record a trace entry for cache-hit turns so the trace DB
            # has a row for every turn the frontend shows.
            async with _open_node(_NODE_TRACE_STORE, "cache_replay", depth=0):
                attach_tag("cache_hit", cache_hit_kind or "exact")
            cached_text = cached["answer"]
            # Annotate so the reviewer sees that this is a replay, not a
            # fresh run — keeps the answer faithful to the original (no
            # silent staleness) while saving the orchestrator round-trip.
            if cache_hit_kind == "near_duplicate":
                note = (
                    f"\n\n*— Reused from a near-duplicate prior question this "
                    f"session ({verdict.near_duplicate_reason or 'matched on subject + scope'}). "
                    f"Original question: \"{verdict.near_duplicate_of}\". "
                    f"No fresh data pull.*"
                )
            else:
                note = (
                    "\n\n*— Reused from a prior identical question this session "
                    "(no fresh data pull).*"
                )
            replayed_text = cached_text + note
            # Replay the prior turn's reasoning trace so the orchestrator-flow
            # / specialists panel populates on a cache hit. Without these
            # emits, the UI receives only `final` + `agent_message` and the
            # reviewer sees an answer appear with no trace of how it was
            # produced — indistinguishable from a silent failure.
            cached_tool_calls = cached.get("tool_calls") or []
            if cached_tool_calls:
                sess.emit("team_plan", {
                    "turn_id": turn_id,
                    "tool_calls": cached_tool_calls,
                })
                replay_ts = int(time.time() * 1000)
                for tc in cached_tool_calls:
                    call_id = tc.get("call_id") or str(uuid.uuid4())
                    sess.emit("agent_started", {
                        "turn_id": turn_id, "call_id": call_id,
                        "tool": tc.get("tool"),
                        "started_at": replay_ts,
                    })
                    sess.emit("agent_completed", {
                        "turn_id": turn_id, "call_id": call_id,
                        "tool": tc.get("tool"),
                        "payload": tc.get("payload"),
                        "duration_ms": tc.get("duration_ms", 0),
                    })
            # Re-emit any charts the original turn produced, scoped to THIS
            # turn's id so the reasoning-trace panel shows them on the replay.
            # Without this, the cached-answer replay would render with no charts
            # — a regression vs the previous "charts inlined in answer_text"
            # behavior, since charts now live as separate SSE events.
            for c in cached.get("charts") or []:
                sess.emit("chart", {**c, "turn_id": turn_id})
            ts = int(time.time() * 1000)
            sess.emit("final", {
                "turn_id": turn_id, "answer": replayed_text,
                "flags": (cached.get("flags") or []) + ["cached_answer_replay"],
                "timeline": [],
                "data_pull_request": cached.get("data_pull_request"),
            })
            sess.emit("agent_message", {
                "id": str(uuid.uuid4()), "role": "agent",
                "text": replayed_text,
                "timestamp": ts, "turn_id": turn_id,
            })
            sess.emit("turn_done", {
                "turn_id": turn_id, "ended_at": ts,
                "duration_ms": ts - self.started_at, "outcome": "ok",
            })
            # A REPLAYED TURN MUST STILL BECOME THE SESSION'S MOST RECENT TURN.
            # `turn_seq` is the only ordering `episodic.build_records` trusts,
            # and it is bumped ONLY by `_store_cached_qa`; the LRU touch inside
            # `_get_cached_qa` reorders the dict but leaves `turn_seq` alone.
            # So without this, the question the reviewer just asked never enters
            # the episodic transcript under its own wording — and the NEXT
            # subject-less follow-up ("think harder", "why is that?", "what
            # contradicts it?") coreferences against whichever turn last
            # actually RAN, which is a different question than the one on the
            # reviewer's screen. That is a silent wrong-antecedent bug: the
            # answer is coherent, just about something else.
            #
            # On an exact hit this rewrites the same key with a fresh seq (it
            # becomes newest). On a near-duplicate it creates an entry under the
            # NEW question's key, so the reviewer's actual wording is what the
            # transcript carries. Stores the ORIGINAL answer text, not
            # `replayed_text` — the "reused from a prior question" line is a
            # display decoration, and re-appending it on a second replay would
            # compound it.
            # Guarded: this runs AFTER `turn_done`, so a bookkeeping failure
            # here must not raise into the outer handler and emit a second
            # terminal event for a turn the reviewer already saw complete.
            try:
                _store_cached_qa(sess, cache_key, {
                    **cached,
                    "origin_question": verdict.redacted_question,
                    "turn_id_origin": turn_id,
                    "answer": cached_text,
                    "replay_of": cached.get("turn_id_origin"),
                }, asked_at=self.turn_started_at)
            except Exception as exc:  # noqa: BLE001
                sess.logger.log("qa_cache_replay_store_failed", {
                    "turn_id": turn_id, "error": repr(exc),
                })
            self.turn_timer.summary(outcome="qa_cache_hit", cache_hit_kind=cache_hit_kind)
            return True
        return False

    # ── Phase 2: build a fresh orchestrator for this turn ─────────────────

    async def _assemble_input(self) -> None:
        sess = self.sess
        turn_id = self.turn_id
        timer_t0 = time.perf_counter()
        orchestrator = Orchestrator(
            llm=None, logger=sess.logger, registry=None,
            pillar=PILLAR, pillar_config=sess.pillar_yaml,
            catalog=sess.catalog, gateway=sess.gateway,
            clients=sess.clients,
        )
        self.orchestrator = orchestrator
        case_folder = _REPORTS_DIR / sess.case_id
        # AppContext is per-turn, but two of its attributes (`_specialist_kps` and
        # `_distiller`) need to outlive a single turn. We pass:
        #   • specialist_kps: a SHARED REFERENCE to the session's KP dict — mutating
        #     it from inside agent_tool persists to the next turn automatically.
        #   • distiller: the orchestrator's distiller_agent (stateless), used by
        #     agent_tool for second-pass KP extraction.
        #   • turn_id: stamped onto each KP at distill time for audit / chronology.
        # Emit hook for tools that want to publish typed SSE events DURING the
        # run (not just at end-of-turn). Used today by `make_chart` to fire a
        # `chart_pending` event the moment a specialist starts plotting, so the
        # UI shows "working on plots" placeholders long before the actual
        # `chart` event lands. The closure stamps `turn_id` so tool callers
        # don't have to. Guard against `sess.emit` raising (closed connection,
        # etc.) so a streaming failure to one client never poisons the agent
        # run for the rest of the session.
        # Amem handle + config are attached to the session at bootstrap by
        # server._get_or_create_session. Read them off `sess` — do NOT
        # `import server` here: server.py runs its gateway/init_tools bootstrap
        # at MODULE TOP LEVEL, so importing it a second time (this module runs
        # under __main__ when launched via `python server.py`) re-runs that
        # bootstrap and rebinds tools.data_tools._gateway to a fresh, UNSCOPED
        # gateway — which silently breaks every live-data lookup. (Regression
        # from the earlier lazy `import server`; fixed here.)
        _amem = getattr(sess, "amem", None)
        _amem_cfg = getattr(sess, "amem_cfg", None)
        # Accumulate-then-compact working set. specialist_kps persists across
        # turns (restored from the node_trace snapshot on reopen) and grows as
        # the distiller adds KPs. We touch Amem only to: (a) SEED an empty set
        # once (cheap list, e.g. fresh case / cleared snapshot), or (b) COMPACT
        # when it has grown past ACTIVE_KP_THRESHOLD (K1) — one hybrid search
        # down to the ACTIVE_KP_KEEP (K2 << K1) most-relevant KPs. Between those
        # there is NO per-turn Amem load — just in-RAM accumulation — so the
        # ~3s search fires only ~every (K1-K2)/kps_per_turn turns.
        if _amem is not None and _amem_cfg is not None and sess.case_id:
            try:
                kps = sess.specialist_kps if isinstance(sess.specialist_kps, dict) else {}
                if not kps:
                    kps = load_case_kps(_amem, _amem_cfg, case_id=sess.case_id)
                # `_qa_turn_seq` counts turns in THIS SESSION; the KP it stamps
                # is per-CASE and durable, so KPs seeded from Amem (or restored
                # from a snapshot whose qa_cache didn't survive) can carry seq
                # values from earlier sessions that outrank anything this
                # session will produce for the next N turns — which would pin
                # the OLDEST KPs as "newest" in the compaction below. Move the
                # counter above whatever we loaded. Skipped numbers are
                # harmless: episodic ordering is relative, not contiguous.
                _seen_seq = max_kp_seq(kps)
                if _seen_seq > getattr(sess, "_qa_turn_seq", 0):
                    sess._qa_turn_seq = _seen_seq
                n = sum(len(v) for v in kps.values())
                if n > ACTIVE_KP_THRESHOLD:
                    compacted = await load_active_kps(
                        _amem, _amem_cfg, case_id=sess.case_id,
                        question=self.verdict.redacted_question,
                        limit=ACTIVE_KP_KEEP)
                    if compacted:      # empty (timeout/miss) → keep accumulating
                        # Union, don't replace: Amem ranks on similarity with no
                        # recency term, so a bare swap can discard the KPs the
                        # previous turn just produced. Result is bounded at ~2*K2.
                        merged = merge_recent_kps(compacted, kps, keep=ACTIVE_KP_KEEP)
                        n_relevance = sum(len(v) for v in compacted.values())
                        n_after = sum(len(v) for v in merged.values())
                        sess.logger.log("active_kp_compacted", {
                            "turn_id": self.turn_id, "n_before": n,
                            "n_after": n_after,
                            "n_relevance": n_relevance,
                            "n_recent_kept": n_after - n_relevance,
                            "k1": ACTIVE_KP_THRESHOLD, "k2": ACTIVE_KP_KEEP})
                        kps = merged
                sess.specialist_kps = kps
            except Exception:
                pass
        ctx = AppContext(
            gateway=sess.gateway,
            case_folder=case_folder,
            logger=sess.logger,
            _specialist_kps=sess.specialist_kps,
            _distiller=getattr(orchestrator, "distiller_agent", None),
            _turn_id=turn_id,
            # The seq this turn will be assigned when `_store_cached_qa` bumps
            # the counter at end of turn. Stamped onto every KP created now, so
            # "newest" is decidable without relying on list order or on
            # `_turn_id` (random hex — unsortable).
            _turn_seq=getattr(sess, "_qa_turn_seq", 0) + 1,
            _emit_event=self._emit_event,
            _node_trace_store=_NODE_TRACE_STORE,
            _catalog=sess.catalog,
            _amem=_amem,
            _amem_cfg=_amem_cfg,
            _case_id=sess.case_id,
            _session_id=sess.session_id,
        )
        self.ctx = ctx
        self.turn_timer.record(
            "orchestrator_context_build",
            int((time.perf_counter() - timer_t0) * 1000),
        )

        # Phase 3 — KP-warmth signal. When specialists have accumulated KPs from
        # earlier turns, prepend a one-line hint to the user question so the
        # orchestrator's team_construction step has a runtime signal that nudges
        # toward reusing warm specialists on in-domain follow-ups. The hint is
        # informational only — the orchestrator retains LLM judgment.
        timer_t0 = time.perf_counter()
        # Past the episodic window, inject the durable Amem case summary as the
        # condensed "older context": the recent EPISODIC_TURNS turns are shown
        # verbatim by the episodic block, everything before that is summarized.
        case_summary = ""
        _amem = getattr(ctx, "_amem", None)
        _amem_cfg = getattr(ctx, "_amem_cfg", None)
        if (_amem is not None and _amem_cfg is not None and sess.case_id
                and getattr(sess, "_qa_turn_seq", 0) > EPISODIC_TURNS):
            case_summary = load_case_summary(_amem, _amem_cfg, case_id=sess.case_id)
            if case_summary:
                sess.logger.log("case_summary_injected", {
                    "turn_id": self.turn_id, "chars": len(case_summary),
                    "episodic_turns": EPISODIC_TURNS})
        framed_question = assemble_orchestrator_input(
            sess, self.verdict, ctx, case_summary=case_summary)
        self.framed_question = framed_question
        # Cases with no curated reports must work smoothly: tell the orchestrator
        # up front not to require report_agent (and skip the backstop below), so
        # a fresh case answers from specialist data without a wasted — and, on
        # safechain, failure-prone — report_agent round.
        self.has_reports = _case_has_reports(sess.case_id)
        if not self.has_reports:
            framed_question = f"{framed_question}\n\n{_NO_REPORTS_NOTE}"
            sess.logger.log("case_has_no_reports", {"turn_id": turn_id})
        else:
            framed_question = f"{framed_question}\n\n{_REPORTS_PRESENT_NOTE}"
        self.run_input = framed_question

        # Each turn starts fresh — no accumulated conversation history.
        # Follow-up context is carried by:
        #   - KP warmth hint (which specialists have cached data)
        #   - specialist_kps (accessible via kp_lookup/kp_list_topics tools)
        #   - qa_cache (exact-match replay for duplicate questions)
        # This keeps input size constant across turns instead of growing.
        self.turn_timer.record(
            "memory_framing",
            int((time.perf_counter() - timer_t0) * 1000),
            warmth_hint_present=bool(sess.specialist_kps and any(sess.specialist_kps.values())),
        )

        # Cooperative-cancellation checkpoint #2 (pre-orchestrator). Catches
        # the case where the user rewound between screen-done and
        # orchestrator-start — abort before kicking off the (potentially
        # 4-minute) orchestrator run.
        if sess.cancel_in_flight.is_set():
            raise _TurnAborted("rewind before orchestrator start")

    # ── Phase 3: stream the orchestrator run ──────────────────────────────

    async def _run_orchestrator(self) -> bool:
        """Drive the orchestrator stream (with the retry loop + planning
        watchdog). Returns True to continue to review/finalize; returns False
        when the orchestrator-error fallback already emitted a terminal
        `final` + `turn_done` for this turn."""
        sess = self.sess
        turn_id = self.turn_id
        ctx = self.ctx
        orchestrator = self.orchestrator
        # Mark when we hand off to the orchestrator so we can measure the gap
        # to the first tool call. That gap = the orchestrator's first LLM call
        # (the team-construction decision); when the user reports "slow to
        # arrive at team construction", THIS is the number to look at.
        orch_t0 = time.time()
        orch_perf_t0 = time.perf_counter()
        sess.logger.log("turn_phase_orchestrator_starting", {
            "turn_id": turn_id,
            "warmth_hint_present": bool(sess.specialist_kps and any(sess.specialist_kps.values())),
            "n_specialists_warm": sum(1 for kps in sess.specialist_kps.values() if kps),
        })

        # Retry loop: on ``ModelBehaviorError`` (typically a malformed FinalAnswer
        # — truncated JSON, schema validation failure, hallucinated tool name)
        # try the orchestrator run ONCE more before falling through to the
        # `_synthesize_fallback_answer` salvage path. Re-running re-invokes
        # specialists, but the per-specialist conversation history persists in
        # ``ctx._specialist_histories`` so warm specialists return faster on the
        # second pass, and the KP ``ctx._specialist_kps`` keeps round-1 KPs
        # available to the orchestrator's prompt.
        #
        # Frontend coordination: an ``orchestrator_retry`` SSE event fires
        # between attempts so the UI can reset the previous attempt's
        # mid-stream state (team_plan, agent_started) and replace it with
        # the new attempt's events under the same turn_id.
        _MAX_ORCH_ATTEMPTS = 2  # ModelBehaviorError: 1 initial + 1 retry
        # Dispatch is MANDATORY, so the "answered with zero tool calls" case
        # gets a LARGER budget with an escalating directive — R1 planning is
        # cheap (~3s) and an ungrounded answer is far worse than one extra
        # round. (The budget is NOT sized for a transport that lacks native
        # `tool_choice`; both transports enforce it natively — see
        # `_NO_TOOLS_RETRY_DIRECTIVE`.)
        _MAX_DISPATCH_ATTEMPTS = 4
        _orch_attempt = 0
        while True:
            if _orch_attempt > 0:
                # Reset per-attempt SSE-emit state and the structured payload
                # accumulator. The cross-attempt state (specialist_kps,
                # specialist_histories on ctx) is intentionally preserved —
                # warm specialists return faster on retry.
                self.call_index_by_id.clear()
                self.tool_calls.clear()
                self.started_at_by_call.clear()
                self.team_plan_emitted = False
                self.first_tool_call_logged = False
                self.specialist_errors_emitted = 0
                self.final_answer = None
            try:
                async with _open_node(_NODE_TRACE_STORE, "orchestrator", depth=0) as orch_nt:
                    attach_tag("streaming")
                    # Stash the input on the wrapper row so clicking the
                    # `orchestrator` row in the viewer shows what was first
                    # asked. The per-LLM-call detail (round_1, round_2, …)
                    # is captured by NodeTraceRunHooks below.
                    if isinstance(self.run_input, list):
                        attach_io(messages_json=json.dumps(self.run_input, default=str))
                        attach_usage(prompt_excerpt=json.dumps(self.run_input, default=str)[:4000])
                    else:
                        attach_io(messages_json=json.dumps(
                            [{"role": "user", "content": str(self.run_input)}],
                            default=str,
                        ))
                        attach_usage(prompt_excerpt=str(self.run_input))
                    # Per-LLM-round capture via SDK hooks. Replaces the
                    # contextvar-propagation path that doesn't reach the
                    # streamed Runner's background task. Hooks fire on the
                    # same task as the model call, so they always see the
                    # right parent.
                    trace_hooks = (
                        NodeTraceRunHooks(_NODE_TRACE_STORE, orch_nt)
                        if isinstance(orch_nt, NodeTrace) else None
                    )
                    streamed = Runner.run_streamed(
                        orchestrator.orchestrator_agent, self.run_input, context=ctx,
                        hooks=trace_hooks,
                    )
                    self.streamed = streamed
                    _orch_perf_t0 = time.perf_counter()
                    _ttft_recorded = False
                    # Round-1 planning watchdog: bound time-to-first-tool-call and
                    # retry on stall. Disarm on the FINAL attempt (no deadline) so a
                    # genuinely slow-but-working env waits rather than hard-failing.
                    _plan_deadline = (
                        time.monotonic() + _ORCH_PLAN_TIMEOUT_S
                        if _orch_attempt + 1 < _MAX_ORCH_ATTEMPTS else None
                    )
                    _stream_iter = streamed.stream_events().__aiter__()
                    while True:
                        try:
                            event = await _next_planning_event(
                                _stream_iter, _plan_deadline, self.first_tool_call_logged
                            )
                        except StopAsyncIteration:
                            break
                        except _PlanningTimeout:
                            # Cancel the stalled run so its background task can't
                            # later emit a ghost team for a turn we've moved past.
                            try:
                                streamed.cancel()
                            except Exception:  # noqa: BLE001
                                pass
                            raise
                        if not _ttft_recorded:
                            attach_latency(
                                ttft_ms=int((time.perf_counter() - _orch_perf_t0) * 1000),
                            )
                            _ttft_recorded = True
                        if event.type != "run_item_stream_event":
                            continue
                        item = event.item
                        (
                            self.team_plan_emitted,
                            self.first_tool_call_logged,
                            _last_agent_completed_at,
                        ) = map_run_item(
                            item, sess=sess, turn_id=turn_id, orch_t0=orch_t0,
                            tool_calls=self.tool_calls,
                            call_index_by_id=self.call_index_by_id,
                            started_at_by_call=self.started_at_by_call,
                            team_plan_emitted=self.team_plan_emitted,
                            first_tool_call_logged=self.first_tool_call_logged,
                            safe_dump=self._safe_dump,
                            drain_specialist_errors=self._drain_specialist_errors,
                            run_ms_by_tool=getattr(
                                self.ctx, "_specialist_run_ms", None),
                        )
                        if _last_agent_completed_at is not None:
                            last_agent_completed_at = _last_agent_completed_at

                # Drain complete — pull the final structured output. The
                # gap between the last `agent_completed` and HERE is the
                # orchestrator's synthesis pass — the model generating the
                # FinalAnswer JSON. Log it as its own phase so slow
                # synthesis on simple questions is diagnosable from the
                # JSONL alone (was previously invisible — synthesis time
                # was bundled into `duration_ms` on `turn_done` along with
                # specialist runtime + end-of-turn drain).
                try:
                    _last = locals().get("last_agent_completed_at")
                    if isinstance(_last, (int, float)):
                        synth_ms = int((time.time() - _last) * 1000)
                        sess.logger.log("turn_phase_synthesis_done", {
                            "turn_id": turn_id,
                            "duration_ms": synth_ms,
                            "n_tool_calls": len(self.tool_calls),
                        })
                except Exception:
                    pass
                final_raw = streamed.final_output
                try:
                    self.final_answer = redact_payload(final_raw) if final_raw else None
                except Exception:
                    self.final_answer = final_raw
                # NOTE: the orchestrator's per-LLM-round detail is captured
                # by NodeTraceRunHooks (passed into Runner.run_streamed
                # above). No need for synthetic team_construction / synthesis
                # rows anymore — round_1 IS team-construction, the last
                # round IS synthesis, both with real durations + full I/O.

                # No conversation history accumulation — each turn starts
                # fresh. Follow-up context lives in specialist_kps + warmth hint.

                # Guard: if orchestrator emitted zero tool calls (skipped
                # specialists entirely), retry — a model-behaviour flake where
                # `tool_choice="required"` did not take, on either transport.
                if (not self.tool_calls
                        and _orch_attempt + 1 < _MAX_DISPATCH_ATTEMPTS):
                    _orch_attempt += 1
                    sess.logger.log("orchestrator_retry_no_tools", {
                        "turn_id": turn_id,
                        "attempt": _orch_attempt,
                        "max_dispatch_attempts": _MAX_DISPATCH_ATTEMPTS,
                    })
                    # Escalate rather than re-run verbatim: append a hard
                    # directive so the retry actually dispatches a specialist.
                    # run_input is normally the framed-question STRING; support a
                    # message list too, defensively. Append once — across
                    # multiple dispatch retries the directive stays present
                    # rather than piling up duplicate copies.
                    if isinstance(self.run_input, list):
                        if not any(isinstance(m, dict)
                                   and m.get("content") == _NO_TOOLS_RETRY_DIRECTIVE
                                   for m in self.run_input):
                            self.run_input = self.run_input + [
                                {"role": "user", "content": _NO_TOOLS_RETRY_DIRECTIVE},
                            ]
                    elif _NO_TOOLS_RETRY_DIRECTIVE not in str(self.run_input):
                        self.run_input = f"{self.run_input}\n\n{_NO_TOOLS_RETRY_DIRECTIVE}"
                    self.tool_calls = []
                    continue

                # Successful attempt — exit the retry loop.
                break

            except _PlanningTimeout:
                # Round-1 stalled. Count the attempt, tell the UI we're replanning,
                # and loop — the next attempt issues a fresh planning call (which,
                # per the heavy-traffic pattern, almost always returns fast). The
                # final attempt runs with the deadline disarmed, so we never hard
                # fail an env that's merely slow.
                _orch_attempt += 1
                self.turn_timer.record(
                    "orchestrator_plan_timeout",
                    int((time.perf_counter() - orch_perf_t0) * 1000),
                    attempt=_orch_attempt,
                    timeout_s=_ORCH_PLAN_TIMEOUT_S,
                )
                sess.logger.log("orchestrator_plan_timeout", {
                    "turn_id": turn_id,
                    "attempt": _orch_attempt,
                    "timeout_s": _ORCH_PLAN_TIMEOUT_S,
                })
                sess.emit("orchestrator_retry", {
                    "turn_id": turn_id,
                    "attempt": _orch_attempt,
                    "reason": "planning_timeout",
                    "message": (
                        "Replanning — team planning was slow to respond; "
                        "retrying with a fresh request."
                    ),
                })
                continue

            except AgentsException as exc:
                # User cancelled mid-run. An aborted SDK run can surface as an
                # AgentsException (e.g. the background run task raising as it
                # unwinds) rather than a clean CancelledError. Do NOT retry and
                # do NOT synthesize the "could not produce a synthesized answer"
                # fallback — the reviewer stopped waiting. Re-raise as an abort so
                # the outer handler emits the clean "Interrupted" path and the
                # rewind restores the pre-question state.
                if sess.cancel_in_flight.is_set():
                    raise _TurnAborted("cancelled during orchestrator run") from exc

                # Retry-once on ``ModelBehaviorError`` BEFORE falling through to
                # the fallback synthesis. A malformed FinalAnswer (truncated
                # JSON / schema validation failure / hallucinated tool name)
                # often clears on a fresh roll because the model's output is
                # non-deterministic. Other AgentsException subclasses (UserError,
                # guardrail tripwires) are not retried — they reflect a real
                # protocol or configuration problem, not transient malformity.
                # A ModelBehaviorError with NO specialists dispatched yet is the
                # orchestrator SKIPPING DISPATCH: the required-round enforcement
                # rejects its stray direct answer, which surfaces here as an
                # unparseable FinalAnswer (prose/markdown, not JSON).
                # Treat it like the no-tools case — the LARGER dispatch budget
                # plus the escalating "call a specialist" directive, not a
                # verbatim re-run (which just reproduces the same skip → 1 retry
                # → fallback, the `orchestrator_attempt_failed attempt=1` symptom).
                _dispatch_phase = not self.tool_calls
                _cap = _MAX_DISPATCH_ATTEMPTS if _dispatch_phase else _MAX_ORCH_ATTEMPTS
                if (
                    isinstance(exc, ModelBehaviorError)
                    and _orch_attempt + 1 < _cap
                ):
                    _orch_attempt += 1
                    self.turn_timer.record(
                        "orchestrator_attempt_failed",
                        int((time.perf_counter() - orch_perf_t0) * 1000),
                        attempt=_orch_attempt,
                        exception_type=type(exc).__name__,
                    )
                    sess.logger.log("orchestrator_retry", {
                        "turn_id": turn_id,
                        "attempt": _orch_attempt,
                        "exception_type": type(exc).__name__,
                        "message": str(exc)[:300],
                        "dispatch_phase": _dispatch_phase,
                        "n_tool_calls_completed": sum(
                            1 for c in self.tool_calls if "payload" in c
                        ),
                    })
                    if _dispatch_phase:
                        # Escalate — force a specialist dispatch on the retry.
                        if isinstance(self.run_input, list):
                            if not any(isinstance(m, dict)
                                       and m.get("content") == _NO_TOOLS_RETRY_DIRECTIVE
                                       for m in self.run_input):
                                self.run_input = self.run_input + [
                                    {"role": "user", "content": _NO_TOOLS_RETRY_DIRECTIVE},
                                ]
                        elif _NO_TOOLS_RETRY_DIRECTIVE not in str(self.run_input):
                            self.run_input = f"{self.run_input}\n\n{_NO_TOOLS_RETRY_DIRECTIVE}"
                    sess.emit("orchestrator_retry", {
                        "turn_id": turn_id,
                        "attempt": _orch_attempt,
                        "reason": "dispatch_skip" if _dispatch_phase else "model_behavior_error",
                        "message": (
                            "Retrying — the orchestrator answered without "
                            "dispatching a specialist; re-prompting to dispatch."
                            if _dispatch_phase else
                            "Retrying — the model's FinalAnswer didn't parse "
                            "(typically a transient JSON malformity)."
                        ),
                    })
                    continue  # back to top of while loop → reset state + rerun

                # Drain any specialist-level failures recorded before the orchestrator
                # itself died so the reviewer still sees what broke under the hood.
                self._drain_specialist_errors()

                # Two failure modes converge here:
                #   • ModelBehaviorError — the model emitted text the SDK couldn't
                #     parse as FinalAnswer (truncated JSON, pseudo tool-call text,
                #     output-schema mismatch). The specialists' work IS valid; only
                #     the final synthesis is broken. Recoverable.
                #   • Other AgentsException — UserError, guardrail tripwires, etc.
                #     Generally not recoverable but we still surface what we have.
                is_model_behavior = isinstance(exc, ModelBehaviorError)
                kind = "model_behavior" if is_model_behavior else type(exc).__name__

                # Human-readable error for the SSE `error` event — strip the noisy
                # Pydantic v2 paragraph so the UI shows something a reviewer can act
                # on, not a 600-char schema dump.
                raw = str(exc)
                if is_model_behavior:
                    short = (
                        "Orchestrator could not produce a valid final answer "
                        "(the model's output was malformed or truncated). "
                        "Returning a partial summary built from the specialists' "
                        "results that did succeed."
                    )
                else:
                    short = f"Orchestrator failed: {type(exc).__name__}: {raw.splitlines()[0][:200]}"

                sess.logger.log("orchestrator_exception", {
                    "turn_id": turn_id,
                    "exception_type": type(exc).__name__,
                    "message": raw[:1000],
                    "kind": kind,
                    "n_tool_calls_completed": sum(1 for c in self.tool_calls if "payload" in c),
                })
                sess.emit("turn_error", {
                    "turn_id": turn_id,
                    "message": short,
                    "kind": kind,
                    "recoverable": is_model_behavior,
                })

                # Build a fallback FinalAnswer from whatever specialists DID return.
                # Without this the reviewer would see "(no answer produced)" plus the
                # raw exception, and the work the specialists did would be wasted.
                answer_text, fallback_flags = _synthesize_fallback_answer(
                    tool_calls=self.tool_calls, error_kind=kind, error_message=raw,
                )
                flags = list(fallback_flags)

                # Append per-specialist failures and any protocol violations to flags
                # the same way the success path does, so the audit trail is uniform.
                specialist_failures = getattr(ctx, "_specialist_errors", None) or []
                for e in specialist_failures:
                    flags.append(
                        f"specialist '{e['specialist']}' failed: "
                        f"{e['error_type']}: {e['error_message']}"
                    )

                # Replay the completed specialists' traces BEFORE `final` so the
                # reasoning panel keeps the work that succeeded — otherwise the
                # turn-wide error renders a correct specialist as "failed" with no
                # trace (alternate_paths_must_replay_full_sse).
                _replay_completed_specialists(sess, turn_id, self.tool_calls)

                # Same reason as the replay above: this branch returns before
                # `_finalize`, so any chart a specialist already plotted is
                # never emitted and its `chart_pending` placeholder is never
                # retracted — the reviewer gets an error message beside a Plots
                # panel that loads forever.
                self._emit_charts_and_retract_pending(
                    turn_id, reason="turn_ended_before_charts_were_built")

                ts = int(time.time() * 1000)
                sess.emit("final", {
                    "turn_id": turn_id, "answer": answer_text, "flags": flags,
                    "timeline": [], "data_pull_request": None,
                })
                sess.emit("agent_message", {
                    "id": str(uuid.uuid4()), "role": "agent", "text": answer_text,
                    "timestamp": ts, "turn_id": turn_id,
                })
                sess.emit("turn_done", {
                    "turn_id": turn_id, "ended_at": ts,
                    "duration_ms": ts - self.started_at,
                    "outcome": "orchestrator_error_fallback" if is_model_behavior
                               else "orchestrator_error",
                })
                # Register the turn in episodic memory WITHOUT making it
                # replayable. The reviewer saw a normal-looking answer here, so
                # the next turn must know this exchange happened — otherwise a
                # subject-less follow-up ("think harder", "what contradicts
                # it?") binds to whichever turn last reached the cache, and
                # answers a different question coherently. But the answer
                # itself is PARTIAL (synthesis failed; this text was assembled
                # from whatever specialists returned), so `no_replay` keeps
                # `_get_cached_qa` from ever serving it again, and
                # `partial_answer` tells the next turn not to treat it as
                # settled. Guarded: this runs after `turn_done`, so a
                # bookkeeping failure must not raise a second terminal event.
                try:
                    _degraded = set(
                        getattr(ctx, "_degraded_specialists", None) or {})
                    _store_cached_qa(sess, self.cache_key, {
                        "answer": answer_text,
                        "flags": flags,
                        "data_pull_request": None,
                        "turn_id_origin": turn_id,
                        "origin_question": self.verdict.redacted_question,
                        "charts": [],
                        "tool_calls": _cacheable_tool_calls(
                            self.tool_calls, _degraded),
                        "no_replay": True,
                        "partial_answer": True,
                    }, asked_at=self.turn_started_at)
                except Exception as exc:  # noqa: BLE001
                    sess.logger.log("qa_cache_fallback_store_failed", {
                        "turn_id": turn_id, "error": repr(exc),
                    })
                self.turn_timer.summary(
                    outcome="orchestrator_error_fallback" if is_model_behavior
                    else "orchestrator_error",
                    n_tool_calls=len(self.tool_calls),
                )
                return False

        self.turn_timer.record(
            "orchestrator_stream",
            int((time.perf_counter() - orch_perf_t0) * 1000),
            n_tool_calls=len(self.tool_calls),
            n_attempts=_orch_attempt + 1,
        )

        # Drain any errors that landed after the last tool call (e.g., a parallel
        # specialist that recorded its failure between the final agent_completed
        # and stream end).
        self._drain_specialist_errors()
        return True

    # ── Phase 3.5: coherence review + bounded re-dispatch ─────────────────

    async def _run_redispatch_pass(self, resume_input) -> "FinalAnswer | None":
        """Phase-3 resume: re-run the orchestrator seeded with the phase-1
        transcript + the injected review directive. Re-emits SSE events for
        any NEW tool calls (alternate_paths_must_replay_full_sse) and
        returns the re-synthesized FinalAnswer (or None on failure)."""
        sess = self.sess
        turn_id = self.turn_id
        ctx = self.ctx
        orchestrator = self.orchestrator
        try:
            async with _open_node(
                _NODE_TRACE_STORE, "orchestrator_redispatch", depth=0
            ) as rnt:
                rhooks = (
                    NodeTraceRunHooks(_NODE_TRACE_STORE, rnt)
                    if isinstance(rnt, NodeTrace) else None
                )
                rstream = Runner.run_streamed(
                    orchestrator.orchestrator_agent, resume_input,
                    context=ctx, hooks=rhooks,
                )
                async for ev in rstream.stream_events():
                    if ev.type != "run_item_stream_event":
                        continue
                    it = ev.item
                    rraw = getattr(it, "raw_item", None)
                    if isinstance(it, ToolCallItem):
                        nm = (
                            getattr(rraw, "name", None)
                            or (rraw.get("name") if isinstance(rraw, dict) else None)
                            or "?"
                        )
                        cid = (
                            getattr(rraw, "call_id", None)
                            or (rraw.get("call_id") if isinstance(rraw, dict) else None)
                            or str(uuid.uuid4())
                        )
                        astr = (
                            getattr(rraw, "arguments", None)
                            or (rraw.get("arguments") if isinstance(rraw, dict) else "{}")
                        )
                        try:
                            aargs = json.loads(astr) if isinstance(astr, str) else (astr or {})
                        except json.JSONDecodeError:
                            aargs = {"raw": astr}
                        sq = (aargs.get("sub_question") or aargs.get("input")
                              or json.dumps(aargs, default=str))
                        self.call_index_by_id[cid] = len(self.tool_calls)
                        self.tool_calls.append({"call_id": cid, "tool": nm, "sub_question": sq})
                        self.started_at_by_call[cid] = int(time.time() * 1000)
                        sess.emit("team_plan", {"turn_id": turn_id,
                                                "tool_calls": list(self.tool_calls)})
                        sess.emit("agent_started", {
                            "turn_id": turn_id, "call_id": cid, "tool": nm,
                            "started_at": self.started_at_by_call[cid],
                        })
                    elif isinstance(it, ToolCallOutputItem):
                        cid = (
                            getattr(rraw, "call_id", None)
                            or (rraw.get("call_id") if isinstance(rraw, dict) else None)
                            or "?"
                        )
                        tl = self.tool_calls[self.call_index_by_id[cid]]["tool"] \
                            if cid in self.call_index_by_id else "?"
                        pl = self._safe_dump(it.output)
                        sts = self.started_at_by_call.get(cid, int(time.time() * 1000))
                        dms = int(time.time() * 1000) - sts
                        # Same correction as the main stream path in sse.py:
                        # parallel siblings share stream timestamps, so prefer
                        # the specialist's own measured wall-clock.
                        _m = (getattr(self.ctx, "_specialist_run_ms", None) or {}).get(tl)
                        if _m:
                            dms = _m.pop(0)
                        if cid in self.call_index_by_id:
                            self.tool_calls[self.call_index_by_id[cid]]["payload"] = pl
                            self.tool_calls[self.call_index_by_id[cid]]["duration_ms"] = dms
                        sess.emit("agent_completed", {
                            "turn_id": turn_id, "call_id": cid, "tool": tl,
                            "payload": pl, "duration_ms": dms,
                        })
                        self._drain_specialist_errors()
            rfinal = rstream.final_output
            try:
                return redact_payload(rfinal) if rfinal else None
            except Exception:  # noqa: BLE001
                return rfinal
        except Exception as exc:  # noqa: BLE001
            sess.logger.log("review_redispatch_failed", {
                "turn_id": turn_id,
                "exception_type": type(exc).__name__,
                "message": str(exc)[:300],
            })
            return None

    async def _review_and_redispatch(self) -> None:
        # ── 3.5 Server-enforced coherence review + bounded re-dispatch ─────
        # Design §4/§5.2/§6; SDK mechanic per 2026-07-03-phased-run-spike.md.
        #
        # ONLY multi-specialist turns (≥2 domain specialists dispatched) enter
        # this block — the single-specialist path above is left completely
        # untouched (no reviewer, no extra Runner run → zero added latency).
        #
        # The whole block is guarded: ANY failure degrades to the phase-1
        # `final_answer` we already have, so this can never make the turn worse
        # than today's behavior. `_run_review` itself already returns None on any
        # error; the try/except here fences the re-dispatch resume as well.
        #
        # Task 7 (early qualified-release via on_tool_end + straggler
        # cancellation) hangs off the SAME helpers — the seam is intentional.
        sess = self.sess
        turn_id = self.turn_id
        ctx = self.ctx
        self.review_flags = []
        if _is_multi_specialist_turn(ctx):
            # The initial dispatch that already ran counts as dispatch round 1.
            if _dispatch_count(ctx) < 1:
                ctx._dispatch_count = 1

            new_final, review_flags = await _apply_review_directive(
                sess=sess,
                ctx=ctx,
                framed_question=self.framed_question,
                tool_calls=self.tool_calls,
                streamed=self.streamed,
                turn_id=turn_id,
                run_redispatch_pass_fn=self._run_redispatch_pass,
            )
            self.review_flags = review_flags
            if new_final is not None:
                self.final_answer = new_final
            self._drain_specialist_errors()

    # ── Phase 3.6: guarantee report_agent ran ─────────────────────────────

    async def _ensure_report_agent(self) -> None:
        """report_agent is MANDATORY on every answered turn — it is the curated-
        report lookup, and a "not covered by the reports" result is a required,
        auditable outcome, not a reason to skip. The orchestrator LLM
        occasionally omits it (typically on terse extraction/list questions)
        despite the PROTOCOL rule; when it does, re-run one round to call it and
        re-synthesize the FinalAnswer so its finding is reflected.

        Fires only when report_agent is genuinely absent (rare), so the extra
        round is paid on those turns alone. Runs AFTER the coherence review so a
        review re-dispatch that already called report_agent isn't duplicated.
        Best-effort: any failure degrades to the answer we already have.
        """
        # Nothing to enforce on empty/no-dispatch turns (screen-reject already
        # returned earlier) or if we somehow have no stream to resume from.
        if self.final_answer is None or not self.tool_calls or self.streamed is None:
            return
        if any(c.get("tool") == "report_agent" for c in self.tool_calls):
            return
        # No curated reports for this case → don't force report_agent. There is
        # nothing to look up, so re-dispatching it only burns a round (and risks
        # a spurious safechain failure). The answer stands on specialist data.
        if not getattr(self, "has_reports", True):
            self.sess.logger.log("report_agent_backstop_skipped_no_reports", {
                "turn_id": self.turn_id,
            })
            return
        try:
            self.sess.logger.log("report_agent_backstop_fired", {
                "turn_id": self.turn_id,
                "n_tool_calls": len(self.tool_calls),
            })
            resume_input = self.streamed.to_input_list()
            resume_input.append({"role": "user", "content": _REPORT_AGENT_DIRECTIVE})
            new_final = await self._run_redispatch_pass(resume_input)
            if new_final is not None:
                self.final_answer = new_final
                self.review_flags.append(
                    "report_agent was dispatched server-side (the initial "
                    "round omitted it)."
                )
            self._drain_specialist_errors()
        except Exception as exc:  # noqa: BLE001 — never wedge the turn on the backstop
            try:
                self.sess.logger.log("report_agent_backstop_failed", {
                    "turn_id": self.turn_id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc)[:200],
                })
            except Exception:  # noqa: BLE001
                pass

    # ── Phase 4: emit final + chat agent message ──────────────────────────

    async def _persist_to_amem(self, answer_text: str) -> None:
        """Durable conversation + case writes. Best-effort; never raises."""
        ctx = self.ctx
        amem = getattr(ctx, "_amem", None)
        cfg = getattr(ctx, "_amem_cfg", None)
        if amem is None or cfg is None:
            return
        sess = self.sess

        # Durable per-specialist conversation records (batched write, one per
        # specialist that ran this turn) — collected during the turn on
        # ctx._specialist_turn_records by agent_tool._runner. Each specialist
        # record holds its sub-question + answer + distilled KPs. Alongside,
        # build the orchestrator's round-1 team dispatch (which specialists
        # were called with what sub-question + concepts).
        # Best-effort like everything else here: a session without a logger
        # (test harnesses) must not break the write this was added to report.
        _amem_log = getattr(sess, "logger", None)
        records = getattr(self.ctx, "_specialist_turn_records", None) or {}
        team_dispatch: list[dict] = []
        for name, rec in records.items():
            kps = kps_for_agent_turn(sess.specialist_kps, name, self.turn_id)
            await write_specialist_memory(
                amem, cfg, case_id=sess.case_id, turn_id=self.turn_id,
                session_id=sess.session_id, agent_id=name,
                sub_question=rec.get("sub_question", ""),
                findings=rec.get("findings", ""),
                kps=kps, tool_calls=rec.get("tool_calls", []),
                logger=_amem_log,
            )
            team_dispatch.append({
                "specialist": name,
                "sub_question": rec.get("sub_question", ""),
                "concepts": rec.get("concepts", []),
            })

        # Orchestrator record: question + team dispatch (round 1) + final
        # answer (round 2). The specialists' distilled KPs live on their own
        # per-specialist records above — they are NOT duplicated here.
        await write_conversation(
            amem, cfg,
            question=self.verdict.redacted_question,
            answer=answer_text,
            case_id=sess.case_id,
            turn_id=self.turn_id,
            session_id=sess.session_id,
            team_dispatch=team_dispatch,
            logger=_amem_log,
        )
        # Refresh the durable case summary periodically (not every turn) — an
        # Amem-side summarization is wasteful per-turn; the recent-N episodic
        # window covers the gap between refreshes.
        seq = getattr(sess, "_qa_turn_seq", 0)
        if seq and seq % _AMEM_CONSOLIDATE_EVERY_N == 0:
            await consolidate_case(amem, cfg, case_id=sess.case_id,
                                   session_id=sess.session_id,
                                   logger=_amem_log)
            # Per-specialist case summaries — each specialist that ran gets a
            # condensed overview of its OWN accumulated findings, but only once
            # it has run in > EPISODIC_TURNS turns (else its own episodic already
            # covers everything). Same cadence as the whole-case summary.
            for name in records:
                await consolidate_agent_case(
                    amem, cfg, case_id=sess.case_id, agent_id=name,
                    session_id=sess.session_id, min_turns=EPISODIC_TURNS,
                    logger=_amem_log)

    def _emit_charts_and_retract_pending(self, turn_id: str, *,
                                         reason: str) -> list[dict]:
        """Emit this turn's charts, then retract every placeholder left over.

        `chart_pending` fires per specialist DURING the turn; `chart` is emitted
        only once the KP is drained. Any pending pair with no chart behind it is
        a card that spins forever, and the frontend cannot detect that on its
        own — nothing else in the stream says the placeholder is dead.

        TWO PATHS NEED THIS, WHICH IS WHY IT IS A METHOD. `_finalize` reaches it
        after a normal turn, where leftovers mean `_collect_turn_charts` deduped
        two specialists' identical figures. The orchestrator-error branch
        returns BEFORE `_finalize`, so a turn whose specialists had already
        plotted used to leave every placeholder hanging — the reviewer saw an
        error and a Plots panel still loading. `reason` is what tells those two
        cases apart in the stream.

        Never raises: this runs on a path that has already emitted `final`, and
        a bookkeeping failure must not raise a second terminal event.
        """
        sess = self.sess
        chart_payloads: list[dict] = []
        try:
            turn_charts = _collect_turn_charts(
                sess.specialist_kps, turn_id, sess.case_id)
            for c in turn_charts or []:
                kp = _find_kp(sess.specialist_kps, c["specialist"],
                              c["topic"], turn_id)
                chart_payloads.append(_build_chart_payload(kp, c))
            for p in chart_payloads:
                sess.emit("chart", {**p, "turn_id": turn_id})
            if chart_payloads:
                sess.logger.log("turn_charts_emitted", {
                    "turn_id": turn_id,
                    "n_charts": len(chart_payloads),
                    "topics": [p["topic"] for p in chart_payloads],
                })
        except Exception:  # noqa: BLE001 — never load-bearing
            pass

        try:
            pending = getattr(self.ctx, "_charts_pending", None) or set()
            emitted = {(p["specialist"], p["topic"]) for p in chart_payloads}
            orphaned = sorted(pending - emitted)
            for spec, topic in orphaned:
                sess.emit("chart_cancelled", {
                    "turn_id": turn_id, "specialist": spec, "topic": topic,
                    "reason": reason,
                })
            if orphaned:
                sess.logger.log("chart_pending_retracted", {
                    "turn_id": turn_id,
                    "n_retracted": len(orphaned),
                    "reason": reason,
                    "retracted": [{"specialist": s, "topic": t}
                                  for s, t in orphaned],
                })
        except Exception:  # noqa: BLE001
            pass
        return chart_payloads

    async def _finalize(self) -> None:
        sess = self.sess
        turn_id = self.turn_id
        ctx = self.ctx
        final_answer = self.final_answer
        review_flags = self.review_flags
        tool_calls = self.tool_calls
        cache_key = self.cache_key
        verdict = self.verdict
        # Cooperative-cancellation checkpoint (post-orchestrator). If the user
        # stopped the turn while the orchestrator/review ran, abort BEFORE
        # emitting anything — especially before the `final_answer is None`
        # salvage below would otherwise publish the "could not produce a
        # synthesized answer" fallback to a reviewer who is no longer waiting.
        # The outer handler renders the clean "Interrupted" path + rewind.
        if sess.cancel_in_flight.is_set():
            raise _TurnAborted("rewind before finalize")
        # Guard: if orchestrator finished with zero tool calls (skipped all
        # specialists), the answer is ungrounded — treat as a failure and
        # log it. Rare on both transports — `tool_choice="required"` is
        # enforced natively by each (measured for safechain, b97375c) — but the
        # model can still return a bare answer, so the guard stays.
        # Report-only turn: `report_agent` ran, no domain specialist did. This
        # is ALLOWED — the PROTOCOL carve-out covers a question whose subject is
        # the curated report itself ("summarize the spending patterns FROM THE
        # REPORT"), where the report is the complete answer. It is logged rather
        # than blocked because the runtime cannot judge whether the question
        # qualified; only the orchestrator can, and the exception is written
        # narrowly. The log is what makes drift auditable: if these turns start
        # showing up for questions about the CASE rather than about the report,
        # the carve-out is being over-applied and the answer is report narrative
        # standing in for live data.
        _domain = [c for c in tool_calls
                   if c.get("tool") not in ("report_agent", "general_specialist")]
        if tool_calls and not _domain and final_answer is not None:
            sess.logger.log("report_only_turn", {
                "turn_id": turn_id,
                "tools": sorted({c.get("tool") for c in tool_calls}),
                "question": getattr(self.verdict, "redacted_question", "")[:160],
                "answer_preview": str(getattr(final_answer, "answer", ""))[:160],
            })

        if not tool_calls and final_answer is not None:
            sess.logger.log("orchestrator_no_tool_calls", {
                "turn_id": turn_id,
                "answer_preview": str(getattr(final_answer, "answer", ""))[:200],
            })
            flags_pre = getattr(final_answer, "flags", []) or []
            flags_pre = list(flags_pre) + [
                "Orchestrator answered without calling any specialist — "
                "answer may be ungrounded. Numbers are NOT verified by live data."
            ]
            if hasattr(final_answer, "flags"):
                final_answer.flags = flags_pre

        if final_answer is None:
            # Orchestrator streamed cleanly but emitted no structured FinalAnswer
            # (e.g., the model returned an empty / non-parseable message that the
            # SDK swallowed). Use the same specialist-output salvage path as the
            # exception branch so the reviewer never sees a bare "(no answer
            # produced)" with the specialists' work thrown away.
            answer_text, fallback_flags = _synthesize_fallback_answer(
                tool_calls=tool_calls,
                error_kind="empty_final_answer",
                error_message="orchestrator produced no FinalAnswer",
            )
            flags: list[str] = list(fallback_flags)
            timeline: list = []
            data_pull = None
        elif hasattr(final_answer, "model_dump"):
            d = final_answer.model_dump()
            answer_text = d.get("answer", "")
            flags = d.get("flags", [])
            timeline = d.get("timeline", [])
            data_pull = d.get("data_pull_request")
        else:
            answer_text = getattr(final_answer, "answer", str(final_answer))
            flags = getattr(final_answer, "flags", [])
            timeline = getattr(final_answer, "timeline", [])
            data_pull = getattr(final_answer, "data_pull_request", None)

        # Fold in any flags produced by the server-enforced coherence review
        # (re-dispatch note / capped-with-residual) so they land in the audit
        # trail + Flags section alongside the synthesized answer.
        if review_flags:
            flags = list(flags or []) + review_flags

        # Server-side provenance: extract report_coverage and specialists from
        # tool_calls so the model doesn't waste tokens restating the full drafts.
        # The FinalAnswer schema tells the model to leave report_draft/team_draft
        # null; we populate provenance from the tool results.
        _AUX_TOOLS_PROV = {"report_agent", "general_specialist"}
        if hasattr(final_answer, "report_draft") and final_answer.report_draft is None:
            for tc in tool_calls:
                if tc.get("tool") == "report_agent" and tc.get("payload"):
                    try:
                        import re as _re
                        cov_match = _re.search(r'"coverage"\s*:\s*"(\w+)"', tc["payload"])
                        if cov_match:
                            from models.types import ReportDraft
                            final_answer.report_draft = ReportDraft(
                                coverage=cov_match.group(1),
                                files_consulted=[],
                            )
                    except Exception:
                        pass
        if hasattr(final_answer, "team_draft") and final_answer.team_draft is None:
            consulted = sorted(
                tc["tool"] for tc in tool_calls if tc["tool"] not in _AUX_TOOLS_PROV
            )
            if consulted:
                from models.types import TeamDraft
                final_answer.team_draft = TeamDraft(
                    answer="", specialists_consulted=consulted,
                )

        # Specialist failure flags — make every wrapper-recorded failure visible
        # in the FinalAnswer so the reviewer sees, e.g., "specialist 'wcc' failed:
        # ModelBehaviorError: invalid JSON …" instead of the silent drop the SDK
        # would otherwise produce.
        specialist_failures = getattr(ctx, "_specialist_errors", None) or []
        if specialist_failures:
            failure_flags = [
                f"specialist '{e['specialist']}' failed: "
                f"{e['error_type']}: {e['error_message']}"
                for e in specialist_failures
            ]
            flags = list(flags or []) + failure_flags

        # (Removed) Orchestrator general_specialist self-invocation protocol checks
        # (skipped / unnecessary / missing-reanswer). Obsolete now that the
        # coherence review is server-enforced (`_run_review` / `_apply_review_directive`)
        # and the orchestrator no longer calls general_specialist. They fired
        # misleading `general_specialist_skipped` on every 2+ specialist turn.
        # `_detect_missing_reanswers` is retained (still unit-tested) but now unused;
        # a later sweep can drop it.

        # ── Emit final answer IMMEDIATELY — don't wait for distiller drain.
        # The user sees the text answer now; charts arrive asynchronously.
        timer_t0 = time.perf_counter()
        ts = int(time.time() * 1000)
        sess.emit("final", {
            "turn_id": turn_id, "answer": answer_text, "flags": flags,
            "timeline": timeline, "data_pull_request": self._safe_dump(data_pull),
        })
        sess.emit("agent_message", {
            "id": str(uuid.uuid4()), "role": "agent", "text": answer_text,
            "timestamp": ts, "turn_id": turn_id,
        })
        self.turn_timer.record(
            "final_sse_emit",
            int((time.perf_counter() - timer_t0) * 1000),
        )

        # ── Drain distiller tasks, then emit charts. The text answer is
        # already visible to the user; charts populate the trace panel
        # asynchronously. If a new question arrives during the drain, the
        # drain timeout (60s) ensures we don't block forever.
        pending = getattr(ctx, "_pending_distillers", None) or []
        timer_t0 = time.perf_counter()
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_DISTILLER_DRAIN_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                sess.logger.log("distiller_drain_timeout", {
                    "turn_id": turn_id,
                    "n_pending": sum(1 for t in pending if not t.done()),
                })
        self.turn_timer.record(
            "distiller_drain",
            int((time.perf_counter() - timer_t0) * 1000),
            n_pending=len(pending),
            n_pending_unfinished=sum(1 for t in pending if not t.done()) if pending else 0,
        )

        # Collect and emit charts from the KP (now populated by the drain).
        timer_t0 = time.perf_counter()
        chart_payloads = self._emit_charts_and_retract_pending(
            turn_id, reason="superseded_by_identical_chart",
        )
        self.turn_timer.record(
            "chart_collect_emit",
            int((time.perf_counter() - timer_t0) * 1000),
            n_charts=len(chart_payloads),
        )

        # ── turn_done after charts are emitted.
        ts = int(time.time() * 1000)
        sess.emit("turn_done", {
            "turn_id": turn_id, "ended_at": ts,
            "duration_ms": ts - self.started_at, "outcome": "ok",
        })

        # Cache the answer for exact-match replay on identical follow-up
        # questions in this session. Skip when the run produced no real answer
        # (final_answer was None) so we don't poison the cache with the
        # "(no answer produced)" sentinel.
        if final_answer is not None and cache_key:
            timer_t0 = time.perf_counter()
            _degraded_names = set(
                getattr(self.ctx, "_degraded_specialists", None) or {})
            evicted_cache_entries = _store_cached_qa(sess, cache_key, {
                "answer": answer_text,
                "flags": list(flags or []),
                "data_pull_request": self._safe_dump(data_pull),
                "turn_id_origin": turn_id,
                # Verbatim question text used by the relevance_check skill on
                # subsequent turns to spot near-duplicates of this one.
                "origin_question": verdict.redacted_question,
                # Chart payloads (turn_id-less) so cached-answer replays can
                # re-emit them under the new turn_id. PNG files persist under
                # reports/<case>/charts/ and serve fine on replay since the URL
                # is unchanged.
                "charts": chart_payloads,
                # Tool-call records (team_plan + each specialist's payload +
                # duration) so a cache-hit replay can repopulate the
                # orchestrator-flow / specialists panel. Without these, the
                # cached-answer replay arrives with an empty reasoning trace
                # and looks like a silent failure to the reviewer.
                "tool_calls": _cacheable_tool_calls(tool_calls, _degraded_names),
            }, asked_at=self.turn_started_at)
            sess.logger.log("qa_cache_store",
                            {"turn_id": turn_id, "norm_q": cache_key,
                             "answer_len": len(answer_text),
                             "entries_now": len(sess.qa_cache),
                             "entries_evicted": evicted_cache_entries})
            # Snapshot CaseSession's cross-turn state to the trace DB so the
            # viewer can show what the conversation "remembers" at end of turn:
            # qa_cache, specialist_kps (with all KnowledgePoints).
            # Failures are swallowed by the store; never breaks the turn.
            if _NODE_TRACE_STORE is not None:
                _conv = getattr(sess, "conversation_id", "") or sess.logger.session_id
                _NODE_TRACE_STORE.snapshot_session(
                    chat_id=_conv,
                    case_id=sess.case_id,
                    turn_id=turn_id,
                    qa_cache=sess.qa_cache,
                    specialist_kps=sess.specialist_kps,
                    conversation_id=_conv,
                    server_run_id=SERVER_RUN_ID,
                    user_id=getattr(sess, "user_id", ""),
                    pillar_id=getattr(sess, "pillar_id", ""),
                )
            await self._persist_to_amem(answer_text)
            self.turn_timer.record(
                "qa_cache_store",
                int((time.perf_counter() - timer_t0) * 1000),
                entries_now=len(sess.qa_cache),
                entries_evicted=evicted_cache_entries,
            )
        self.turn_timer.summary(
            outcome="ok",
            n_tool_calls=len(tool_calls),
            n_charts=len(chart_payloads),
        )

    # ── The readable phase order ──────────────────────────────────────────

    async def run(self) -> None:
        if not await self._screen():
            return
        if await self._replay_from_cache():
            return
        await self._assemble_input()
        if not await self._run_orchestrator():
            return
        # Cooperative-cancellation checkpoints between the post-orchestrator
        # phases. The review re-dispatch and report-agent backstop each issue
        # fresh LLM calls; if the reviewer stopped the turn, skip straight to
        # the clean-abort path instead of spending more work + emitting a stale
        # answer. `_finalize` carries the final guard before it emits anything.
        if self.sess.cancel_in_flight.is_set():
            raise _TurnAborted("rewind after orchestrator")
        await self._review_and_redispatch()
        if self.sess.cancel_in_flight.is_set():
            raise _TurnAborted("rewind after coherence review")
        await self._ensure_report_agent()
        await self._finalize()
