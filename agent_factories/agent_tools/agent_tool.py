"""Wraps an Agent as a tool with PII redaction on input + output boundaries."""
from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from agents import Agent, RunContextWrapper, Runner, function_tool
from agents.exceptions import AgentsException, MaxTurnsExceeded

from logger.process_timer import ProcessTimer
from llm.firewall_stack import LLM_CALL_KIND, redact_payload, sanitize_message
from tools.node_trace import _open_node, attach_extra, attach_tag
from agent_factories.agent_tools.series_extract import _extract_data_tool_outputs
from agent_factories.agent_tools.distiller_pass import _distill_and_persist
from agent_factories.agent_tools.auto_chart import _auto_chart_from_tool_outputs
from agent_factories.agent_tools.specialist_input_tool import (
    _SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES,
    _ELIDED_SPECIALIST_TOOL_OUTPUT,
    _compact_specialist_history,
    assemble_specialist_input,
)


# Inner-specialist turn budget. SDK default is 10. Lowered from 15 → 6
# after measuring real traces: data specialists were consistently using
# 2-3 rounds with the system_prompt batching guidance, and the rare
# 4th round was almost always over-exploration that didn't improve the
# answer. 6 gives a small safety margin for genuinely hard questions
# while shaving ~25-30s off the wall-clock outliers. Pair with the
# "emit final output ASAP" rule in data_query.md — together they
# discourage the model from looping past a clear answer.
_SPECIALIST_MAX_TURNS = 6

# Wall-clock budget per specialist call. Bounds hangs from stalled LLM /
# transport layers that ``max_turns`` alone can't catch. 240s is generous
# vs. the typical 20-90s specialist run, but well below the user-perceived
# "is this thing broken?" threshold so we surface the failure instead of
# letting the SSE stream stall.
_SPECIALIST_TIMEOUT_S = float(os.environ.get("SPECIALIST_TIMEOUT_S", "240"))


def _record_failure(app_ctx, name: str, sub_question: str,
                    error_type: str, message: str, exc: BaseException | None) -> str:
    """Log + persist a specialist failure, return the structured payload the
    orchestrator sees in place of the SpecialistOutput JSON.

    Two consumers read what we record here:
      • The orchestrator LLM gets the returned string and can decide whether
        to fall back (call a different specialist, mark a data_gap, narrow
        the sub-question, etc.). The ``[FAILED ...]`` sentinel lets it
        recognize the response as a failure and not as content to synthesize.
      • The server stream loop drains ``app_ctx._specialist_errors`` to emit
        typed ``error`` SSE events and to append flags to the FinalAnswer,
        so the reviewer sees the actual cause instead of a silent drop.
    """
    logger = getattr(app_ctx, "logger", None)
    if logger is not None:
        logger.log("specialist_call_failed", {
            "specialist": name,
            "error_type": error_type,
            "error_message": message,
            "sub_question": sub_question[:500],
            # Truncated traceback only — full one is reproducible from the
            # error_type + message and would bloat the JSONL.
            "traceback_tail": (traceback.format_exc().splitlines()[-1]
                               if exc is not None else ""),
        })
    errors = getattr(app_ctx, "_specialist_errors", None)
    if isinstance(errors, list):
        errors.append({
            "specialist": name,
            "error_type": error_type,
            "error_message": message,
            "sub_question": sub_question,
        })
    return (
        f"[FAILED {name}] {error_type}: {message}\n"
        f"This specialist could not produce a SpecialistOutput for this "
        f"sub-question. Treat as a data_gap for this domain — proceed with "
        f"other specialists' findings and note the failure in your flags. "
        f"If retry is appropriate, narrow the sub-question (e.g., limit to "
        f"a single metric or period)."
    )


def _normalize_subq(text: str) -> str:
    """Collapse whitespace + lowercase a sub-question for the per-AppContext
    dedup cache. Two sub-questions with trivial wording differences ('Did
    the customer have any returns?' vs 'did the customer have any returns')
    map to the same key.
    """
    return " ".join((text or "").strip().lower().split())


def _extract_tool_calls(result) -> list[dict]:
    """Extract {func, params} for each function call the specialist made this
    run, from `result.to_input_list()`. Mirrors the style of
    `series_extract._extract_data_tool_outputs`, which reads the paired
    `function_call_output` items — this reads the `function_call` items
    (func name + JSON-parsed arguments, no payloads). Best-effort: items
    without a name are skipped; unparseable arguments fall back to
    `{"_raw": arguments}` rather than dropping the call."""
    out: list[dict] = []
    if not hasattr(result, "to_input_list"):
        return out
    try:
        items = result.to_input_list()
    except Exception:
        return out
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        func_name = item.get("name")
        if not func_name:
            continue
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            try:
                params = json.loads(arguments)
            except Exception:
                params = {"_raw": arguments}
        else:
            params = arguments or {}
        out.append({"func": func_name, "params": params})
    return out


def agent_tool(
    agent: Agent,
    name: str,
    description: str,
    *,
    timeout_s: float = _SPECIALIST_TIMEOUT_S,
    max_turns: int = _SPECIALIST_MAX_TURNS,
    catalog=None,
    data_hints: list[str] | None = None,
):
    """Expose an Agent as a tool, with PII redaction on the input and output boundaries.

    ``timeout_s`` / ``max_turns`` override the per-specialist budgets. They
    default to the domain-specialist values (240s / 6 turns); auxiliary agents
    like ``report_agent`` (a shallow file lookup, not analysis) should pass a
    much tighter budget so a stalled LLM round fails fast and best-effort
    instead of dragging the whole turn toward the 240s fence.

    Inter-agent transit boundary: anything flowing in (LLM-generated sub-
    question) gets ``sanitize_message``; anything flowing out (the inner
    agent's final output) gets ``redact_payload``.

    Multi-turn behavior: when ``ctx.context`` carries a
    ``_specialist_histories`` dict (see ``AppContext``), this wrapper reads
    the entry keyed by ``name`` to find the specialist's prior conversation
    and prepends it to the new sub-question on each call. After the run,
    the updated history (``result.to_input_list()``) is saved back. So a
    follow-up tool call to the same specialist within the same AppContext
    sees what the specialist already asked / answered, instead of starting
    fresh. Reset by constructing a new AppContext.
    """
    inner = agent

    @function_tool(name_override=name, description_override=description)
    async def _runner(ctx: RunContextWrapper, sub_question: str,
                      concepts: list[str] | None = None) -> str:
        runner_started = time.perf_counter()
        redacted_in = sanitize_message(sub_question)

        # Look up per-specialist history on the surrounding AppContext.
        # When the context doesn't expose `_specialist_histories` (e.g.
        # tests with a bare context object), behave like the legacy
        # single-turn path.
        app_ctx = ctx.context if ctx else None
        logger = getattr(app_ctx, "logger", None)
        timer = ProcessTimer(
            logger,
            "specialist_call",
            turn_id=getattr(app_ctx, "_turn_id", None),
            specialist=name,
        )
        histories = getattr(app_ctx, "_specialist_histories", None)
        prior = histories.get(name) if isinstance(histories, dict) else None

        # Per-AppContext dedup: same (specialist, sub_question) within the
        # same context returns the cached payload rather than re-running.
        # This caps cost when the orchestrator (especially in safechain mode,
        # where parallel-tool-call semantics aren't native) emits the same
        # call multiple times in one turn with trivial wording variations.
        cache_key = (name, _normalize_subq(redacted_in))
        seen = getattr(app_ctx, "_specialist_call_cache", None)
        if seen is None and app_ctx is not None:
            try:
                seen = {}
                # Attach lazily so each AppContext gets its own cache; tests
                # with a bare SimpleNamespace tolerate the attr add.
                app_ctx._specialist_call_cache = seen  # type: ignore[attr-defined]
            except Exception:
                seen = None
        if isinstance(seen, dict) and cache_key in seen:
            cached = seen[cache_key]
            if logger is not None:
                logger.log("specialist_call_dedup_hit",
                           {"specialist": name,
                            "sub_question_norm": cache_key[1]})
            # Tag the active (parent / orchestrator) node so optimization
            # reports can surface dedup hit-rate without re-deriving it.
            attach_tag("specialist_dedup_hit")
            timer.summary(
                outcome="dedup_hit",
                total_ms=int((time.perf_counter() - runner_started) * 1000),
                sub_question_chars=len(redacted_in),
            )
            return cached

        # Programmatic HARD GATE: block general_specialist when < 2 domain
        # specialists ran this turn. The orchestrator prompt says this but
        # the model sometimes ignores it; this enforces it server-side.
        _NON_DOMAIN = {"report_agent", "general_specialist"}
        domain_called = getattr(app_ctx, "_domain_specialists_called", None)
        if name not in _NON_DOMAIN:
            if isinstance(domain_called, set):
                domain_called.add(name)
        elif name == "general_specialist":
            n_domain = len(domain_called) if isinstance(domain_called, set) else 0
            if n_domain < 2:
                if logger is not None:
                    logger.log("general_specialist_blocked", {
                        "reason": "fewer_than_2_domain_specialists",
                        "domain_specialists_called": sorted(domain_called)
                        if isinstance(domain_called, set) else [],
                    })
                timer.summary(
                    outcome="blocked",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return (
                    "[SKIPPED — only 1 domain specialist called. "
                    "Emit FinalAnswer NOW from the specialist + report_agent outputs.]"
                )

        # KB digest preface — the specialist's accumulated knowledge from
        # earlier turns. Only prepend on the FIRST call within this turn (no
        # intra-turn `prior` exists yet); on subsequent within-turn calls the
        # `prior` transcript already carries the digest from the first call's
        # input message, so re-prepending would duplicate it.
        contextual_in = redacted_in
        kb_digest_n_kps = 0
        if not prior:
            contextual_in, kb_digest_n_kps = assemble_specialist_input(
                app_ctx, name, redacted_in, concepts, catalog, data_hints, logger)

        # Inject the case folder file list for report_agent so it can decide
        # the layout: use the report_needle concept→file table to pick 1-2
        # curated files, or fs_grep(terms) to locate content in a long
        # consolidated file, then fs_read_file (optionally a line-range slice)
        # rather than reading everything.
        if name == "report_agent" and not prior:
            case_folder = getattr(app_ctx, "case_folder", None)
            if (case_folder is not None
                    and hasattr(case_folder, "exists") and case_folder.exists()):
                files = sorted(
                    p.name for p in case_folder.iterdir()
                    if p.is_file() and p.suffix in (".md", ".txt", ".csv")
                )
                if files:
                    file_list = ", ".join(files)
                    contextual_in = (
                        f"[Report files in case folder: {file_list}. "
                        f"To read a file, call the fs_read_file tool with "
                        f'filename="<name>".]\n\n'
                        f"{contextual_in}"
                    )

        if prior:
            run_input = prior + [{"role": "user", "content": contextual_in}]
        else:
            run_input = contextual_in
        timer.record(
            "specialist_context_prepare",
            int((time.perf_counter() - runner_started) * 1000),
            has_prior=bool(prior),
            kb_digest_prepended=contextual_in != redacted_in,
            sub_question_chars=len(redacted_in),
            run_input_items=len(run_input) if isinstance(run_input, list) else 1,
        )

        # Specialist run with retry on ModelBehaviorError. SafeChain can
        # truncate long JSON outputs, causing parse failures on the first
        # attempt. A retry often succeeds because the model produces a
        # shorter response. Max 2 attempts (1 initial + 1 retry).
        _MAX_SPECIALIST_ATTEMPTS = 2
        result = None
        last_exc = None
        node_store = getattr(app_ctx, "_node_trace_store", None)
        node_label = name if name == "report_agent" else f"specialist.{name}"

        for _attempt in range(_MAX_SPECIALIST_ATTEMPTS):
            try:
                t0 = time.perf_counter()
                kind_token = LLM_CALL_KIND.set("specialist")
                try:
                    label = node_label if _attempt == 0 else f"{node_label}.retry"
                    async with _open_node(node_store, label, depth=0):
                        if _attempt == 0:
                            if kb_digest_n_kps:
                                attach_tag("kb_digest_present")
                                attach_extra(n_kps_in_digest=kb_digest_n_kps)
                            if prior:
                                attach_tag("warm_specialist")
                        else:
                            attach_tag("retry")
                        result = await asyncio.wait_for(
                            Runner.run(
                                inner, run_input, context=app_ctx,
                                max_turns=max_turns,
                            ),
                            timeout=timeout_s,
                        )
                finally:
                    LLM_CALL_KIND.reset(kind_token)
                timer.record(
                    "specialist_runner",
                    int((time.perf_counter() - t0) * 1000),
                    max_turns=max_turns,
                    attempt=_attempt,
                )
                break  # success
            except MaxTurnsExceeded as exc:
                timer.summary(
                    outcome="failed",
                    error_type="max_turns_exceeded",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                    "max_turns_exceeded",
                    f"hit the {max_turns}-turn budget — "
                    f"partial findings were not returned. {exc}",
                    exc,
                )
            except asyncio.TimeoutError as exc:
                timer.summary(
                    outcome="failed",
                    error_type="timeout",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                    "timeout",
                    f"specialist did not complete within "
                    f"{timeout_s:.0f}s wall-clock budget.",
                    exc,
                )
            except asyncio.CancelledError:
                timer.summary(
                    outcome="cancelled",
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                raise  # let the turn-level cancellation propagate
            except AgentsException as exc:
                last_exc = exc
                if _attempt + 1 < _MAX_SPECIALIST_ATTEMPTS:
                    if logger is not None:
                        logger.log("specialist_retry", {
                            "specialist": name,
                            "attempt": _attempt,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:200],
                        })
                    continue  # retry
                timer.summary(
                    outcome="failed",
                    error_type=type(exc).__name__,
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                    type(exc).__name__,
                    str(exc) or "no message",
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 - last-resort fence
                timer.summary(
                    outcome="failed",
                    error_type=type(exc).__name__,
                    total_ms=int((time.perf_counter() - runner_started) * 1000),
                )
                return _record_failure(
                    app_ctx, name, redacted_in,
                type(exc).__name__,
                str(exc) or repr(exc),
                exc,
            )

        # Persist the updated history so the next call to this specialist
        # in the same context picks up where we left off.
        if isinstance(histories, dict) and hasattr(result, "to_input_list"):
            t0 = time.perf_counter()
            next_history = result.to_input_list()
            next_history, history_stats = _compact_specialist_history(next_history)
            histories[name] = next_history
            timer.record(
                "specialist_history_compact",
                int((time.perf_counter() - t0) * 1000),
                **history_stats,
            )
            if history_stats["items_elided"]:
                if logger is not None:
                    logger.log("specialist_history_compacted", {
                        "specialist": name,
                        **history_stats,
                        "kept_recent_user_messages":
                            _SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES,
                    })

        # Guard: Runner.run can complete without exception yet leave
        # final_output=None when the SDK's output-type parser silently
        # fails (observed on the SafeChain path where truncated JSON
        # repairs into valid-but-schemeless content). Without this check
        # the tool returns None → UI shows "no result" and the
        # orchestrator synthesizes from incomplete specialist data.
        if result is None or getattr(result, "final_output", None) is None:
            timer.summary(
                outcome="failed",
                error_type="empty_final_output",
                total_ms=int((time.perf_counter() - runner_started) * 1000),
            )
            return _record_failure(
                app_ctx, name, redacted_in,
                "empty_final_output",
                "Runner.run completed but final_output is None — "
                "the model likely produced content that did not match "
                "the SpecialistOutput schema.",
                last_exc,
            )

        t0 = time.perf_counter()
        try:
            payload = redact_payload(result.final_output)
        except Exception as exc:  # noqa: BLE001
            # Output redaction failure is rare but should not look like a
            # silent drop. Surface it the same way as a run failure.
            timer.summary(
                outcome="failed",
                error_type=f"redact_{type(exc).__name__}",
                total_ms=int((time.perf_counter() - runner_started) * 1000),
            )
            return _record_failure(
                app_ctx, name, redacted_in,
                f"redact_{type(exc).__name__}",
                f"output redaction failed: {exc}",
                exc,
            )
        # Inject the sub-question into the payload so the orchestrator
        # (and general_specialist reading the outputs) knows what each
        # specialist was answering — without the specialist wasting output
        # tokens to echo it. Replaces the removed SpecialistOutput.question
        # field with a zero-cost server-side injection.
        if isinstance(payload, str) and name != "report_agent":
            payload = f"[Sub-question: {redacted_in}]\n{payload}"

        timer.record(
            "specialist_output_redact",
            int((time.perf_counter() - t0) * 1000),
            payload_chars=len(payload) if isinstance(payload, str) else 0,
        )

        # Second pass — distill knowledge points from the (un-redacted)
        # SpecialistOutput. We FIRE AND FORGET so the orchestrator receives
        # the specialist's payload immediately (no distiller round-trip on
        # the critical path). Server.py awaits all pending distillers at
        # end-of-turn so the KB is fully populated before the next turn's
        # warmth digest is built.
        pending = getattr(app_ctx, "_pending_distillers", None)
        t0 = time.perf_counter()
        try:
            tool_outputs = _extract_data_tool_outputs(result)
            if logger is not None and name != "report_agent":
                logger.log("distiller_tool_outputs_extracted", {
                    "specialist": name,
                    "tool_outputs_chars": len(tool_outputs),
                    "n_items": len(result.to_input_list()) if hasattr(result, "to_input_list") else -1,
                })
            # Stash this specialist's turn record (sub-question, findings,
            # tool calls) on the AppContext for the batched end-of-turn
            # durable write (conductor._persist_to_amem). report_agent is
            # excluded — it's a file lookup, not a data specialist.
            if name != "report_agent" and isinstance(
                getattr(app_ctx, "_specialist_turn_records", None), dict
            ):
                _findings = getattr(result.final_output, "findings", "") or ""
                app_ctx._specialist_turn_records[name] = {
                    "sub_question": redacted_in,
                    "concepts": list(concepts or []),
                    "findings": _findings,
                    "tool_calls": _extract_tool_calls(result),
                }
            # Fire TWO parallel async tasks:
            # 1. Distiller: extract claims into KB for follow-ups
            # 2. Auto-chart: render charts from tool outputs (no LLM needed)
            task = asyncio.create_task(
                _distill_and_persist(
                    app_ctx, name, redacted_in, result.final_output,
                    tool_outputs=tool_outputs,
                ),
                name=f"distill-{name}",
            )
            if isinstance(pending, list):
                pending.append(task)
            # Auto-chart: parse tool outputs for series data, render charts
            if name != "report_agent" and tool_outputs:
                chart_task = asyncio.create_task(
                    _auto_chart_from_tool_outputs(
                        app_ctx, name, tool_outputs,
                    ),
                    name=f"autochart-{name}",
                )
                if isinstance(pending, list):
                    pending.append(chart_task)
        except Exception as exc:  # noqa: BLE001 - belt-and-suspenders
            if logger is not None:
                logger.log("distiller_outer_failure", {
                    "specialist": name,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                })
        timer.record(
            "distiller_schedule",
            int((time.perf_counter() - t0) * 1000),
            pending_distillers=len(pending) if isinstance(pending, list) else None,
        )

        if isinstance(seen, dict):
            seen[cache_key] = payload
        timer.summary(
            outcome="ok",
            total_ms=int((time.perf_counter() - runner_started) * 1000),
            sub_question_chars=len(redacted_in),
        )
        return payload

    return _runner
