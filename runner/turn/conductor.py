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
import time
import uuid
from typing import Any

from agents import Runner
from agents.exceptions import AgentsException, ModelBehaviorError
from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem

from agent_factories.app_context import AppContext
from llm.firewall_stack import redact_payload
from logger.process_timer import ProcessTimer
from models.types import FinalAnswer
from runner.orchestrator import Orchestrator
from runner.turn.input_assembly import assemble_orchestrator_input
from runner.turn.review import (
    _apply_review_directive, _dispatch_count, _is_multi_specialist_turn,
)
from tools.node_trace import (
    NodeTrace, NodeTraceRunHooks, TURN_SCOPE, TurnScope,
    _open_node, attach_io, attach_latency, attach_tag, attach_usage,
)

# Server-local helpers, constants, and the planning-watchdog helpers reused
# verbatim. `server.py` imports THIS module only lazily (inside the thin
# `_run_turn_streamed` wrapper), so there is no module-load cycle: importing
# `server` here always finds it fully defined. `_PlanningTimeout` /
# `_next_planning_event` stay defined in `server.py` (tests reference them as
# `server._next_planning_event`) and are imported here for use in the stream
# drive.
from server import (  # noqa: E402
    PILLAR,
    _NODE_TRACE_STORE,
    _ORCH_PLAN_TIMEOUT_S,
    _PRIOR_QUESTIONS_FOR_SCREEN,
    _REPORTS_DIR,
    _SCREEN_TIMEOUT_S,
    _PlanningTimeout,
    _TurnAborted,
    _collect_turn_charts,
    _find_kp,
    _get_cached_qa,
    _next_planning_event,
    _normalize_q,
    _replay_completed_specialists,
    _store_cached_qa,
    _synthesize_fallback_answer,
)


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
        TURN_SCOPE.set(TurnScope(
            chat_id=sess.logger.session_id,
            case_id=sess.case_id,
            turn_id=turn_id,
        ))
        self.turn_timer = ProcessTimer(
            sess.logger,
            "turn",
            turn_id=turn_id,
            case_id=sess.case_id,
        )
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
                verdict = await asyncio.wait_for(
                    sess.chat_agent.screen(self.question, prior_questions=prior_questions),
                    timeout=_SCREEN_TIMEOUT_S,
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
            self.turn_timer.summary(outcome="qa_cache_hit", cache_hit_kind=cache_hit_kind)
            return True
        return False

    # ── Phase 2: build a fresh orchestrator for this turn ─────────────────

    def _assemble_input(self) -> None:
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
        # AppContext is per-turn, but two of its attributes (`_specialist_kb` and
        # `_distiller`) need to outlive a single turn. We pass:
        #   • specialist_kb: a SHARED REFERENCE to the session's KB dict — mutating
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
        ctx = AppContext(
            gateway=sess.gateway,
            case_folder=case_folder,
            logger=sess.logger,
            _specialist_kb=sess.specialist_kb,
            _distiller=getattr(orchestrator, "distiller_agent", None),
            _turn_id=turn_id,
            _emit_event=self._emit_event,
            _node_trace_store=_NODE_TRACE_STORE,
            _catalog=sess.catalog,
        )
        self.ctx = ctx
        self.turn_timer.record(
            "orchestrator_context_build",
            int((time.perf_counter() - timer_t0) * 1000),
        )

        # Phase 3 — KB-warmth signal. When specialists have accumulated KPs from
        # earlier turns, prepend a one-line hint to the user question so the
        # orchestrator's team_construction step has a runtime signal that nudges
        # toward reusing warm specialists on in-domain follow-ups. The hint is
        # informational only — the orchestrator retains LLM judgment.
        timer_t0 = time.perf_counter()
        framed_question = assemble_orchestrator_input(sess, self.verdict, ctx)
        self.framed_question = framed_question
        self.run_input = framed_question

        # Each turn starts fresh — no accumulated conversation history.
        # Follow-up context is carried by:
        #   - KB warmth hint (which specialists have cached data)
        #   - specialist_kb (accessible via kb_lookup/kb_list_topics tools)
        #   - qa_cache (exact-match replay for duplicate questions)
        # This keeps input size constant across turns instead of growing.
        self.turn_timer.record(
            "memory_framing",
            int((time.perf_counter() - timer_t0) * 1000),
            input_history_len=0,
            warmth_hint_present=bool(sess.specialist_kb and any(sess.specialist_kb.values())),
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
            "input_history_len": len(sess.input_history),
            "input_history_chars": sum(
                len(json.dumps(item, default=str)) for item in sess.input_history
            ) if sess.input_history else 0,
            "warmth_hint_present": bool(sess.specialist_kb and any(sess.specialist_kb.values())),
            "n_specialists_warm": sum(1 for kps in sess.specialist_kb.values() if kps),
        })

        # Retry loop: on ``ModelBehaviorError`` (typically a malformed FinalAnswer
        # — truncated JSON, schema validation failure, hallucinated tool name)
        # try the orchestrator run ONCE more before falling through to the
        # `_synthesize_fallback_answer` salvage path. Re-running re-invokes
        # specialists, but the per-specialist conversation history persists in
        # ``ctx._specialist_histories`` so warm specialists return faster on the
        # second pass, and the KB ``ctx._specialist_kb`` keeps round-1 KPs
        # available to the orchestrator's prompt.
        #
        # Frontend coordination: an ``orchestrator_retry`` SSE event fires
        # between attempts so the UI can reset the previous attempt's
        # mid-stream state (team_plan, agent_started) and replace it with
        # the new attempt's events under the same turn_id.
        _MAX_ORCH_ATTEMPTS = 2  # 1 initial + 1 retry
        _orch_attempt = 0
        while True:
            if _orch_attempt > 0:
                # Reset per-attempt SSE-emit state and the structured payload
                # accumulator. The cross-attempt state (specialist_kb,
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
                        raw = getattr(item, "raw_item", None)

                        if isinstance(item, ToolCallItem):
                            name = (
                                getattr(raw, "name", None)
                                or (raw.get("name") if isinstance(raw, dict) else None)
                                or "?"
                            )
                            call_id = (
                                getattr(raw, "call_id", None)
                                or (raw.get("call_id") if isinstance(raw, dict) else None)
                                or str(uuid.uuid4())
                            )
                            args_str = (
                                getattr(raw, "arguments", None)
                                or (raw.get("arguments") if isinstance(raw, dict) else "{}")
                            )
                            try:
                                args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
                            except json.JSONDecodeError:
                                args = {"raw": args_str}
                            sub_q = args.get("sub_question") or args.get("input") or json.dumps(args, default=str)

                            self.call_index_by_id[call_id] = len(self.tool_calls)
                            self.tool_calls.append({"call_id": call_id, "tool": name, "sub_question": sub_q})
                            self.started_at_by_call[call_id] = int(time.time() * 1000)

                            # The first tool call IS team construction — this is the
                            # gap the user reports as "time to team construction stage".
                            if not self.first_tool_call_logged:
                                sess.logger.log("turn_phase_first_tool_call", {
                                    "turn_id": turn_id,
                                    "duration_ms_since_orch_start":
                                        int((time.time() - orch_t0) * 1000),
                                    "first_tool": name,
                                })
                                self.first_tool_call_logged = True

                            # First tool call → emit team_plan once (the orchestrator may add more
                            # later; we send team_plan again on subsequent calls for incremental UX).
                            self.team_plan_emitted = True
                            sess.emit("team_plan", {"turn_id": turn_id, "tool_calls": list(self.tool_calls)})
                            sess.emit("agent_started", {
                                "turn_id": turn_id, "call_id": call_id, "tool": name,
                                "started_at": self.started_at_by_call[call_id],
                            })

                        elif isinstance(item, ToolCallOutputItem):
                            call_id = (
                                getattr(raw, "call_id", None)
                                or (raw.get("call_id") if isinstance(raw, dict) else None)
                                or "?"
                            )
                            tool = "?"
                            if call_id in self.call_index_by_id:
                                tool = self.tool_calls[self.call_index_by_id[call_id]]["tool"]
                            payload = self._safe_dump(item.output)
                            started_ts = self.started_at_by_call.get(call_id, int(time.time() * 1000))
                            duration_ms = int(time.time() * 1000) - started_ts
                            # Stash the payload back onto `tool_calls` so a late-stage
                            # orchestrator failure (ModelBehaviorError on FinalAnswer
                            # parsing, etc.) can still synthesize a partial fallback
                            # answer from the specialists' outputs the reviewer paid for.
                            if call_id in self.call_index_by_id:
                                self.tool_calls[self.call_index_by_id[call_id]]["payload"] = payload
                                self.tool_calls[self.call_index_by_id[call_id]]["duration_ms"] = duration_ms
                            sess.emit("agent_completed", {
                                "turn_id": turn_id, "call_id": call_id, "tool": tool,
                                "payload": payload, "duration_ms": duration_ms,
                            })
                            # If the agent_tool wrapper recorded a failure for any
                            # specialist this run, fan out typed `error` events now so
                            # the UI can show the real cause beside the vague `[FAILED …]`
                            # payload it just received.
                            self._drain_specialist_errors()
                            # Stamp the time of the LAST agent_completed so we can
                            # attribute the gap-to-end-of-stream to synthesis.
                            last_agent_completed_at = time.time()

                        elif isinstance(item, MessageOutputItem):
                            pass  # handled by .final_output below

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
                # fresh. Follow-up context lives in specialist_kb + warmth hint.

                # Guard: if orchestrator emitted zero tool calls (skipped
                # specialists entirely), retry once — this is a safechain
                # flake where tool_choice="required" was ignored.
                if (not self.tool_calls
                        and _orch_attempt + 1 < _MAX_ORCH_ATTEMPTS):
                    _orch_attempt += 1
                    sess.logger.log("orchestrator_retry_no_tools", {
                        "turn_id": turn_id,
                        "attempt": _orch_attempt,
                    })
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
                # Retry-once on ``ModelBehaviorError`` BEFORE falling through to
                # the fallback synthesis. A malformed FinalAnswer (truncated
                # JSON / schema validation failure / hallucinated tool name)
                # often clears on a fresh roll because the model's output is
                # non-deterministic. Other AgentsException subclasses (UserError,
                # guardrail tripwires) are not retried — they reflect a real
                # protocol or configuration problem, not transient malformity.
                if (
                    isinstance(exc, ModelBehaviorError)
                    and _orch_attempt + 1 < _MAX_ORCH_ATTEMPTS
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
                        "n_tool_calls_completed": sum(
                            1 for c in self.tool_calls if "payload" in c
                        ),
                    })
                    sess.emit("orchestrator_retry", {
                        "turn_id": turn_id,
                        "attempt": _orch_attempt,
                        "reason": "model_behavior_error",
                        "message": (
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

    # ── Phase 4: emit final + chat agent message ──────────────────────────

    async def _finalize(self) -> None:
        sess = self.sess
        turn_id = self.turn_id
        ctx = self.ctx
        final_answer = self.final_answer
        review_flags = self.review_flags
        tool_calls = self.tool_calls
        cache_key = self.cache_key
        verdict = self.verdict
        # Guard: if orchestrator finished with zero tool calls (skipped all
        # specialists), the answer is ungrounded — treat as a failure and
        # log it. This happens on safechain where tool_choice="required"
        # is enforced via prompt text and the LLM sometimes ignores it.
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
                    timeout=60.0,
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

        # Collect and emit charts from the KB (now populated by the drain).
        timer_t0 = time.perf_counter()
        turn_charts = _collect_turn_charts(sess.specialist_kb, turn_id, sess.case_id)
        chart_payloads: list[dict] = []
        if turn_charts:
            for c in turn_charts:
                kp = _find_kp(sess.specialist_kb, c["specialist"], c["topic"], turn_id)
                viz = (kp or {}).get("viz") or {}
                payload = {
                    "specialist": c["specialist"],
                    "topic": c["topic"],
                    "url": c["url"],
                    "claim": (kp or {}).get("claim", ""),
                    "source_call": (kp or {}).get("source_call", ""),
                    "kind": viz.get("kind", "") if isinstance(viz, dict) else "",
                    "vega_spec": (kp or {}).get("vega_spec"),
                }
                if payload["kind"] == "table":
                    payload["numbers"] = (kp or {}).get("numbers") or []
                    payload["x_field"] = viz.get("x_field", "") if isinstance(viz, dict) else ""
                    payload["y_fields"] = (
                        viz.get("y_fields") or [] if isinstance(viz, dict) else []
                    )
                chart_payloads.append(payload)
            for p in chart_payloads:
                sess.emit("chart", {**p, "turn_id": turn_id})
            sess.logger.log("turn_charts_emitted", {
                "turn_id": turn_id,
                "n_charts": len(chart_payloads),
                "topics": [p["topic"] for p in chart_payloads],
            })
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
                "tool_calls": [
                    {
                        "call_id": tc.get("call_id"),
                        "tool": tc.get("tool"),
                        "sub_question": tc.get("sub_question"),
                        "payload": tc.get("payload"),
                        "duration_ms": tc.get("duration_ms"),
                    }
                    for tc in tool_calls
                ],
            })
            sess.logger.log("qa_cache_store",
                            {"turn_id": turn_id, "norm_q": cache_key,
                             "answer_len": len(answer_text),
                             "entries_now": len(sess.qa_cache),
                             "entries_evicted": evicted_cache_entries})
            # Snapshot CaseSession's cross-turn state to the trace DB so the
            # viewer can show what the conversation "remembers" at end of turn:
            # qa_cache, specialist_kb (with all KnowledgePoints), input_history.
            # Failures are swallowed by the store; never breaks the turn.
            if _NODE_TRACE_STORE is not None:
                _NODE_TRACE_STORE.snapshot_session(
                    chat_id=sess.logger.session_id,
                    case_id=sess.case_id,
                    turn_id=turn_id,
                    qa_cache=sess.qa_cache,
                    specialist_kb=sess.specialist_kb,
                    input_history=sess.input_history,
                )
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
        self._assemble_input()
        if not await self._run_orchestrator():
            return
        await self._review_and_redispatch()
        await self._finalize()
