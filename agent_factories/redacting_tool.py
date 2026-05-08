"""Wraps an Agent as a tool with PII redaction on input + output boundaries."""
from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path

from agents import Agent, RunContextWrapper, Runner, function_tool
from agents.exceptions import AgentsException, MaxTurnsExceeded

from llm.firewall_stack import redact_payload, sanitize_message
from tools.viz_renderer import kp_to_vega_spec, render_chart


# Inner-specialist turn budget. SDK default is 10, which is too tight for
# data-heavy questions ("spending pattern", "default journey") that require
# schema probe + multiple month-by-month aggregates. 25 covers the normal
# worst case while still bounding runaway loops.
_SPECIALIST_MAX_TURNS = 25

# Wall-clock budget per specialist call. Bounds hangs from stalled LLM /
# transport layers that ``max_turns`` alone can't catch. 240s is generous
# vs. the typical 20-90s specialist run, but well below the user-perceived
# "is this thing broken?" threshold so we surface the failure instead of
# letting the SSE stream stall.
_SPECIALIST_TIMEOUT_S = 240.0

# Wall-clock budget for the second-pass distiller. Distillation is purely
# text-extraction; should be fast. If it stalls, log + skip — the specialist
# answer is already in flight to the orchestrator and we degrade gracefully
# to "no KB update this turn."
_DISTILLER_TIMEOUT_S = 30.0


def _active_kps(kps: list[dict]) -> list[dict]:
    """Latest knowledge point per topic. The underlying list is appended to
    chronologically (never mutated), so iterating in order and keeping the
    last-seen entry per topic gives us the active set. Older entries with
    the same topic remain in the list for audit but are hidden from the
    digest the specialist sees on its next call.
    """
    active: dict[str, dict] = {}
    for kp in kps or []:
        topic = kp.get("topic")
        if topic:
            active[topic] = kp
    return list(active.values())


def _format_kb_digest(kps: list[dict]) -> str:
    """Render the active KP set as a preface the specialist reads before
    answering. Empty string when there's nothing to surface.

    The digest is intentionally short (one line per active KP) — its job is
    to keep the specialist from re-running the same `summarize_trend` call
    when the answer is already on file, NOT to replay every detail. The
    specialist can still re-query when verification is needed.
    """
    active = _active_kps(kps)
    if not active:
        return ""
    lines = [
        "[YOUR KNOWLEDGE BASE — facts established earlier this session.",
        "Refer to these BEFORE re-running queries; only re-query when the new",
        "question goes beyond what's recorded here, or when a value needs",
        "verification.]",
        "",
    ]
    for kp in active:
        confidence = kp.get("confidence") or "medium"
        line = f"- **{kp['topic']}** [{confidence}]: {kp['claim']}"
        src = kp.get("source_call")
        if src:
            line += f"  _via `{src}`_"
        lines.append(line)
    return "\n".join(lines)


async def _distill_and_persist(
    app_ctx, name: str, sub_question: str, specialist_output,
) -> int:
    """Run the distiller agent on a successful SpecialistOutput, append any
    extracted KnowledgePoints to the session KB. Returns count added.

    Failures are logged and non-fatal: the specialist's answer is already
    flowing to the orchestrator regardless. The session KB just doesn't get
    a new entry this turn — the specialist will still answer the next
    question, just without the new fact in its preface digest.
    """
    distiller = getattr(app_ctx, "_distiller", None)
    kb = getattr(app_ctx, "_specialist_kb", None)
    if distiller is None or kb is None:
        return 0  # Not wired — tests / legacy paths skip distillation entirely.

    logger = getattr(app_ctx, "logger", None)

    # Pack a compact, JSON-serializable view of the specialist's output for
    # the distiller's prompt. SpecialistOutput is a Pydantic model on the
    # success path; on failures we'd be a "[FAILED ...]" string, but we
    # only get here on success so that branch is paranoia.
    try:
        if hasattr(specialist_output, "model_dump"):
            output_payload = json.dumps(specialist_output.model_dump(), default=str)
        elif isinstance(specialist_output, str):
            output_payload = specialist_output
        else:
            output_payload = json.dumps(specialist_output, default=str)
    except Exception:
        output_payload = str(specialist_output)

    distiller_input = (
        f"Specialist: {name}\n"
        f"Sub-question: {sub_question}\n\n"
        f"--- SpecialistOutput (JSON) ---\n{output_payload}"
    )

    try:
        result = await asyncio.wait_for(
            Runner.run(distiller, distiller_input, context=app_ctx, max_turns=2),
            timeout=_DISTILLER_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - distillation is best-effort
        if logger is not None:
            logger.log("distiller_failed", {
                "specialist": name,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            })
        return 0

    out = getattr(result, "final_output", None)
    new_kps = getattr(out, "knowledge_points", None) or []
    if not isinstance(new_kps, list):
        return 0

    turn_id = getattr(app_ctx, "_turn_id", None)
    case_folder = getattr(app_ctx, "case_folder", None)
    sess_list = kb.setdefault(name, [])
    added_topics: list[str] = []
    for kp in new_kps:
        try:
            kp_dict = kp.model_dump() if hasattr(kp, "model_dump") else dict(kp)
        except Exception:
            continue
        if turn_id is not None and not kp_dict.get("captured_at_turn"):
            kp_dict["captured_at_turn"] = turn_id

        # Phase 2: render chart + Vega-Lite spec when the KP carries a viz
        # spec with usable numbers. Failures are silent (renderer logs +
        # returns None) so the KP still lands in the KB; the chart just
        # doesn't appear in the agent's answer this turn.
        if isinstance(kp_dict.get("viz"), dict) and kp_dict.get("numbers"):
            spec = kp_to_vega_spec(kp_dict)
            if spec is not None:
                kp_dict["vega_spec"] = spec
            if case_folder is not None:
                charts_dir = Path(case_folder) / "charts"
                img_path = render_chart(
                    kp_dict, charts_dir,
                    turn_id=turn_id, logger=logger,
                )
                if img_path is not None:
                    kp_dict["image_path"] = img_path

        sess_list.append(kp_dict)
        if kp_dict.get("topic"):
            added_topics.append(kp_dict["topic"])

    if added_topics and logger is not None:
        logger.log("distiller_kps_added", {
            "specialist": name,
            "n_added": len(added_topics),
            "kb_size_now": len(sess_list),
            "topics": added_topics,
            "n_with_charts": sum(1 for k in sess_list[-len(added_topics):]
                                 if k.get("image_path")),
        })
    return len(added_topics)


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


def redacting_tool(agent: Agent, name: str, description: str):
    """Return a FunctionTool that runs ``agent`` with input/output redaction.

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
    async def _runner(ctx: RunContextWrapper, sub_question: str) -> str:
        redacted_in = sanitize_message(sub_question)

        # Look up per-specialist history on the surrounding AppContext.
        # When the context doesn't expose `_specialist_histories` (e.g.
        # tests with a bare context object), behave like the legacy
        # single-turn path.
        app_ctx = ctx.context if ctx else None
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
            logger = getattr(app_ctx, "logger", None)
            if logger is not None:
                logger.log("specialist_call_dedup_hit",
                           {"specialist": name,
                            "sub_question_norm": cache_key[1]})
            return cached

        # KB digest preface — the specialist's accumulated knowledge from
        # earlier turns. Only prepend on the FIRST call within this turn (no
        # intra-turn `prior` exists yet); on subsequent within-turn calls the
        # `prior` transcript already carries the digest from the first call's
        # input message, so re-prepending would duplicate it.
        contextual_in = redacted_in
        if not prior:
            kb_obj = getattr(app_ctx, "_specialist_kb", None)
            if isinstance(kb_obj, dict):
                kb_digest = _format_kb_digest(kb_obj.get(name, []))
                if kb_digest:
                    contextual_in = (
                        f"{kb_digest}\n\n--- New question ---\n{redacted_in}"
                    )

        if prior:
            run_input = prior + [{"role": "user", "content": contextual_in}]
        else:
            run_input = contextual_in

        # Wall-clock + turn-budget + exception fence around the inner run.
        # Without these, a hung LLM / network layer or any non-MaxTurnsExceeded
        # SDK error (ModelBehaviorError, output-schema parse failure, transport
        # error) escapes to function_tool's default failure handler, which
        # returns a generic "An error occurred while running the tool" string
        # — the orchestrator then renders it as "specialist did not return"
        # and the reviewer never sees the real cause. We catch each class
        # explicitly, log it, and return a structured ``[FAILED …]`` payload.
        try:
            result = await asyncio.wait_for(
                Runner.run(
                    inner, run_input, context=app_ctx,
                    max_turns=_SPECIALIST_MAX_TURNS,
                ),
                timeout=_SPECIALIST_TIMEOUT_S,
            )
        except MaxTurnsExceeded as exc:
            return _record_failure(
                app_ctx, name, redacted_in,
                "max_turns_exceeded",
                f"hit the {_SPECIALIST_MAX_TURNS}-turn budget — "
                f"partial findings were not returned. {exc}",
                exc,
            )
        except asyncio.TimeoutError as exc:
            return _record_failure(
                app_ctx, name, redacted_in,
                "timeout",
                f"specialist did not complete within "
                f"{_SPECIALIST_TIMEOUT_S:.0f}s wall-clock budget.",
                exc,
            )
        except AgentsException as exc:
            # Covers ModelBehaviorError (malformed JSON / nonexistent tool /
            # output-schema parse failure), UserError (SDK misuse), and
            # guardrail tripwires.
            return _record_failure(
                app_ctx, name, redacted_in,
                type(exc).__name__,
                str(exc) or "no message",
                exc,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort fence
            # Network / transport / serialization / anything else. We don't
            # want a stray exception class to slip past and surface as the
            # SDK's generic paraphrase.
            return _record_failure(
                app_ctx, name, redacted_in,
                type(exc).__name__,
                str(exc) or repr(exc),
                exc,
            )

        # Persist the updated history so the next call to this specialist
        # in the same context picks up where we left off.
        if isinstance(histories, dict) and hasattr(result, "to_input_list"):
            histories[name] = result.to_input_list()

        try:
            payload = redact_payload(result.final_output)
        except Exception as exc:  # noqa: BLE001
            # Output redaction failure is rare but should not look like a
            # silent drop. Surface it the same way as a run failure.
            return _record_failure(
                app_ctx, name, redacted_in,
                f"redact_{type(exc).__name__}",
                f"output redaction failed: {exc}",
                exc,
            )

        # Second pass — distill knowledge points from the (un-redacted)
        # SpecialistOutput. We pass the structured Pydantic object, not the
        # redacted string payload, so the distiller sees full numeric content.
        # Failures are logged + ignored (KB simply doesn't grow this turn).
        try:
            await _distill_and_persist(
                app_ctx, name, redacted_in, result.final_output,
            )
        except Exception as exc:  # noqa: BLE001 - belt-and-suspenders
            logger = getattr(app_ctx, "logger", None)
            if logger is not None:
                logger.log("distiller_outer_failure", {
                    "specialist": name,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                })

        if isinstance(seen, dict):
            seen[cache_key] = payload
        return payload

    return _runner
