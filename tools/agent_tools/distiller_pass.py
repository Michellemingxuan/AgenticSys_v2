"""Distiller second-pass: extract reusable KnowledgePoints from a specialist's
output and persist them to the session KB, filling `numbers` from parsed tool
outputs. Fire-and-forget; scheduled by agent_tool._runner. Extracted from
tools/agent_tool.py (see the decomposition design spec)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

from agents import Runner

from llm.firewall_stack import LLM_CALL_KIND
from logger.process_timer import ProcessTimer
from tools.node_trace import _open_node, attach_extra, attach_tag
from tools.agent_tools.series_extract import _parse_series_from_tool_outputs, _fill_kp_numbers

# Wall-clock budget for the second-pass distiller. Distillation is purely
# text-extraction; should be fast. If it stalls, log + skip — the specialist
# answer is already in flight to the orchestrator and we degrade gracefully
# to "no KB update this turn."
#
# Bumped 30s → 60s after observing real-world timeouts on chunky
# specialists (spend_payments returning ~8 chartable claims at once,
# case 366132845011 turn around 06:20). The distiller timing out kills
# BOTH KB warmth for the next turn AND charts for the current turn (the
# auto-distiller is the primary chart-generation path; make_chart is
# specialist-explicit and proves unreliable when the LLM forgets). 60s
# is still under the slowest specialist budget (240s) so end-of-turn
# drain doesn't blow up.
_DISTILLER_TIMEOUT_S = float(os.environ.get("DISTILLER_TIMEOUT_S", "120"))

# Keywords that signal series/trend/concentration data — the kind the
# distiller actually extracts into chartable KPs. These come from tool
# outputs (summarize_trend summary block, summarize_by_group concentration
# block) and are reliable markers that the output is worth distilling.
# Without these, the specialist used scalar tools (aggregate_column) or
# query_table row dumps — in either case the distiller consistently
# returns empty knowledge_points and wastes 10-20s.
_SERIES_KEYWORDS = frozenset({
    "slope", "peak", "trough", "pct_change", "coefficient",
    "hhi", "top1_share", "top3_share", "concentration",
    "trend", "trajectory", "missing_periods",
})


def _is_narrow_output(specialist_output, sub_question: str = "") -> bool:
    """Detect narrow specialist outputs (simple counts, yes/no) where
    distillation costs 10-20s LLM round-trip but yields 0 knowledge points.

    Two-gate check:
    1. Series keywords in findings+evidence → always distill (the
       distiller extracts chartable KPs from these)
    2. No series keywords + short findings → narrow, skip distiller
    """
    if not hasattr(specialist_output, "findings"):
        return False
    findings = getattr(specialist_output, "findings", "") or ""
    evidence = getattr(specialist_output, "evidence", None) or []

    all_text = (findings + " " + " ".join(
        e for e in evidence if isinstance(e, str)
    )).lower()

    if any(kw in all_text for kw in _SERIES_KEYWORDS):
        return False

    return len(findings) <= 150


async def _distill_and_persist(
    app_ctx, name: str, sub_question: str, specialist_output,
    tool_outputs: str = "",
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
    logger = getattr(app_ctx, "logger", None)
    node_store = getattr(app_ctx, "_node_trace_store", None)

    if distiller is None or kb is None:
        if logger is not None:
            logger.log("distiller_skipped", {
                "specialist": name,
                "reason": "not_wired",
                "distiller_none": distiller is None,
                "kb_none": kb is None,
            })
        return 0

    if name == "report_agent":
        if logger is not None:
            logger.log("distiller_skipped", {
                "specialist": name,
                "reason": "non_specialist_output_shape",
            })
        return 0

    # Narrow outputs → direct KB insertion with a node trace entry
    # so it's visible in the trace viewer.
    if _is_narrow_output(specialist_output, sub_question):
        findings = getattr(specialist_output, "findings", "") or ""
        sq_hash = hashlib.md5(sub_question.encode()).hexdigest()[:8]
        kp_dict = {
            "topic": f"{name}_q_{sq_hash}",
            "claim": findings,
            "numbers": [],
            "source_call": "",
            "confidence": "high",
        }
        turn_id = getattr(app_ctx, "_turn_id", None)
        if turn_id is not None:
            kp_dict["captured_at_turn"] = turn_id
        sess_list = kb.setdefault(name, [])
        sess_list.append(kp_dict)
        if logger is not None:
            logger.log("distiller_direct_kp", {
                "specialist": name,
                "reason": "narrow_output_direct_insert",
                "topic": kp_dict["topic"],
                "claim": findings[:200],
                "turn_id": turn_id,
            })
        # Create a node trace entry so the viewer shows it
        async with _open_node(node_store, f"distiller.{name}", depth=0):
            attach_tag("direct_insert")
            attach_extra(
                topic=kp_dict["topic"],
                claim=findings[:100],
                outcome="direct_insert",
                n_added=1,
            )
        return 1

    timer = ProcessTimer(
        logger,
        "distiller",
        turn_id=getattr(app_ctx, "_turn_id", None),
        specialist=name,
    )

    # Pack a compact, JSON-serializable view of the specialist's output for
    # the distiller's prompt. SpecialistOutput is a Pydantic model on the
    # success path; on failures we'd be a "[FAILED ...]" string, but we
    # only get here on success so that branch is paranoia.
    t0 = time.perf_counter()
    try:
        if hasattr(specialist_output, "model_dump"):
            output_payload = json.dumps(specialist_output.model_dump(), default=str)
        elif isinstance(specialist_output, str):
            output_payload = specialist_output
        else:
            output_payload = json.dumps(specialist_output, default=str)
    except Exception:
        output_payload = str(specialist_output)
    timer.record(
        "distiller_input_serialize",
        int((time.perf_counter() - t0) * 1000),
        payload_chars=len(output_payload),
    )

    distiller_input = (
        f"Specialist: {name}\n"
        f"Sub-question: {sub_question}\n\n"
        f"--- SpecialistOutput (JSON) ---\n{output_payload}"
    )
    # Raw tool outputs are NOT included in the distiller input — they
    # inflate it by 5-10K tokens and add 10-15s TTFT. Instead, the
    # post-fill step (`_fill_kp_numbers`) programmatically fills the
    # `numbers` array from parsed tool outputs after the distiller runs.
    # The distiller only needs the SpecialistOutput to decide topic,
    # claim, viz kind, and confidence.

    try:
        t0 = time.perf_counter()
        # Route distiller LLM calls to the SPECIALIST semaphore pool
        # (12 slots) instead of the orchestrator pool (2 slots). Without
        # this, the distiller and orchestrator synthesis compete for the
        # same 2 slots — serializing what should run in parallel.
        kind_token = LLM_CALL_KIND.set("specialist")
        node_store = getattr(app_ctx, "_node_trace_store", None)
        try:
            async with _open_node(node_store, f"distiller.{name}", depth=0):
                result = await asyncio.wait_for(
                    Runner.run(distiller, distiller_input, context=app_ctx, max_turns=1),
                    timeout=_DISTILLER_TIMEOUT_S,
                )
        finally:
            LLM_CALL_KIND.reset(kind_token)
        timer.record(
            "distiller_runner",
            int((time.perf_counter() - t0) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - distillation is best-effort
        if logger is not None:
            logger.log("distiller_failed", {
                "specialist": name,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            })
        timer.summary(outcome="failed", error_type=type(exc).__name__)
        return 0

    out = getattr(result, "final_output", None)
    new_kps = getattr(out, "knowledge_points", None) or []
    if not isinstance(new_kps, list):
        timer.summary(outcome="no_kps", n_added=0)
        return 0

    # Pre-parse series from tool outputs for post-fill (fills null values
    # the distiller left in `numbers` with real data from tool results).
    parsed_series = _parse_series_from_tool_outputs(tool_outputs)

    turn_id = getattr(app_ctx, "_turn_id", None)
    case_folder = getattr(app_ctx, "case_folder", None)
    sess_list = kb.setdefault(name, [])
    added_topics: list[str] = []
    n_with_charts = 0
    n_nulls_filled = 0
    render_total_ms = 0
    t0 = time.perf_counter()
    for kp in new_kps:
        try:
            kp_dict = kp.model_dump() if hasattr(kp, "model_dump") else dict(kp)
        except Exception:
            continue
        if turn_id is not None and not kp_dict.get("captured_at_turn"):
            kp_dict["captured_at_turn"] = turn_id

        # Post-fill: fill/construct numbers from parsed tool outputs.
        # Mode 1: numbers is empty → construct full array from series.
        # Mode 2: numbers has nulls → fill from matched series.
        has_viz = isinstance(kp_dict.get("viz"), dict) and kp_dict["viz"].get("y_fields")
        if parsed_series and (kp_dict.get("numbers") or has_viz):
            before_len = len(kp_dict.get("numbers") or [])
            before = sum(
                1 for row in kp_dict["numbers"] if isinstance(row, dict)
                for v in row.values() if v is None
            ) if kp_dict.get("numbers") else 0
            _fill_kp_numbers(kp_dict, parsed_series)
            after_len = len(kp_dict.get("numbers") or [])
            after = sum(
                1 for row in kp_dict["numbers"] if isinstance(row, dict)
                for v in row.values() if v is None
            ) if kp_dict.get("numbers") else 0
            n_nulls_filled += (before - after) + (after_len - before_len)
        elif has_viz and not kp_dict.get("numbers") and logger is not None:
            logger.log("distiller_kp_no_numbers", {
                "specialist": name,
                "topic": kp_dict.get("topic", ""),
                "has_parsed_series": bool(parsed_series),
                "n_parsed_series": len(parsed_series),
                "tool_outputs_chars": len(tool_outputs),
            })

        # Charts are now the SPECIALIST's responsibility (via make_chart
        # tool call with real data). The distiller only handles KB warmth
        # (claims/topics for follow-up questions). No chart rendering here.

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
    timer.record(
        "kp_persist_and_render",
        int((time.perf_counter() - t0) * 1000),
        n_kps=len(new_kps),
        n_added=len(added_topics),
        n_with_charts=n_with_charts,
        render_total_ms=render_total_ms,
    )
    timer.summary(
        outcome="ok",
        n_added=len(added_topics),
        n_with_charts=n_with_charts,
        n_nulls_filled=n_nulls_filled,
    )
    return len(added_topics)
