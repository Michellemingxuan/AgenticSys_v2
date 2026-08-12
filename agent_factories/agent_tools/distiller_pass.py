"""Distiller second-pass: extract reusable KnowledgePoints from a specialist's
output and persist them to the session KB, filling `numbers` from parsed tool
outputs. Fire-and-forget; scheduled by agent_tool._runner. Extracted from
tools/agent_tool.py (see the decomposition design spec)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

from agents import Runner

from llm.firewall_stack import LLM_CALL_KIND
from logger.process_timer import ProcessTimer
from tools.node_trace import _open_node, attach_extra, attach_tag
from agent_factories.agent_tools.series_extract import _parse_series_from_tool_outputs, _fill_kp_numbers

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


def _salvage_truncated_kps(exc: Exception) -> list[dict]:
    """Knowledge points recoverable from a response the model cut off.

    WHY. The distiller is asked for every row of every series (H1, "no
    abridging") and told to include a key for each period even when the value
    is null, because a post-fill supplies the real numbers afterwards. A 30-row
    series is ~450 tokens of skeleton, so two or three such KPs run past the
    output budget, the JSON stops mid-array, and the SDK raises
    ModelBehaviorError("Invalid JSON when parsing {…"). That was a TOTAL loss:
    26 of 38 distiller failures in the logs are this, and each one dropped the
    whole turn's knowledge rather than the one KP that got cut.

    The complete objects before the cut are perfectly good. This walks the
    `knowledge_points` array and keeps every object that closed, so truncation
    costs the last KP instead of all of them.

    Best-effort by construction: any parse trouble yields `[]` and the caller
    falls back to the existing failure path.
    """
    text = str(exc)
    marker = '"knowledge_points"'
    start = text.find(marker)
    if start == -1:
        return []
    open_bracket = text.find("[", start)
    if open_bracket == -1:
        return []

    out: list[dict] = []
    depth = 0
    in_str = False
    escaped = False
    obj_start = -1
    for i in range(open_bracket + 1, len(text)):
        ch = text[i]
        if in_str:
            # Order matters: a backslash escapes the NEXT char, so check the
            # escape flag before treating a quote as a terminator.
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                try:
                    kp = json.loads(text[obj_start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    break
                # A KP without a claim is not usable knowledge, and the KB
                # digest renders it as a blank line.
                if isinstance(kp, dict) and kp.get("topic") and kp.get("claim"):
                    out.append(kp)
                obj_start = -1
        elif ch == "]" and depth == 0:
            break
    return out

# How many existing topic slugs to show the distiller so it can reuse one
# instead of forking a near-identical name. Slugs are short (~25 chars), so 40
# costs ~1KB of prompt — cheap next to the 8-18KB SpecialistOutput payload.
_MAX_EXISTING_TOPICS = 40

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

# Narrow-output KPs skip the distiller LLM, so they don't get an LLM-named
# topic. Derive a readable, deterministic topic from the sub-question instead
# of an opaque `{name}_q_<hash>` — so kb_lookup and the KB digest stay
# meaningful. Stopwords are dropped so the slug carries the actual metric.
_TOPIC_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "did", "do", "does", "what",
    "how", "why", "when", "which", "who", "of", "for", "to", "in", "on", "at",
    "and", "or", "over", "this", "that", "there", "any", "all", "its", "it",
    "with", "by", "be", "as", "has", "have", "had", "their", "they", "customer",
    "case", "many", "much", "were",
})


def _slug_topic(sub_question: str, name: str, max_tokens: int = 5) -> str:
    """Readable, deterministic topic slug from a sub-question (narrow-output
    path). Same sub-question → same slug, so KB dedup still works; falls back to
    the old name-scoped hash only when no usable tokens remain."""
    toks = re.findall(r"[a-z0-9]+", (sub_question or "").lower())
    keep = [t for t in toks if t not in _TOPIC_STOPWORDS and len(t) > 1]
    slug = "_".join(keep[:max_tokens])
    if slug:
        return slug
    return f"{name}_q_{hashlib.md5((sub_question or '').encode()).hexdigest()[:8]}"


def _topic_key(topic: str) -> frozenset:
    """Order-insensitive identity of a topic slug: its token set.

    `cdss_tsr_trajectory` and `tsr_cdss_trajectory` are the same topic said two
    ways, but `_active_kps` keys on the exact string — so the second one does
    NOT supersede the first. Both then sit in the digest as separate cached
    topics, and `kb_lookup("cdss_tsr_trajectory")` returns the STALE claim while
    the fresh one hides under a name the specialist has no reason to guess.
    Observed 9x on one topic in case 366132845011.
    """
    return frozenset(t for t in re.split(r"[^a-z0-9]+", (topic or "").lower()) if t)


def _snap_topic(topic: str, existing: list[str]) -> str:
    """Return the prior slug for this topic when the new one is a token
    PERMUTATION of it, else `topic` unchanged.

    Deliberately conservative — exact token-set equality only. Fuzzier matching
    (stemming, synonyms, substrings) risks collapsing genuinely distinct topics
    into one, which silently destroys a claim; the distillation skill's rule is
    that two KPs share a topic only when they answer the SAME question.
    """
    key = _topic_key(topic)
    if not key:
        return topic
    for prior in existing:
        if prior and prior != topic and _topic_key(prior) == key:
            return prior
    return topic


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
        # `_slug_topic` derives the slug from the sub-question's wording, so a
        # re-asked question phrased in a different order yields a permuted slug
        # — same snap as the distiller path.
        from tools.kb_tools import _active_kps as _active
        kp_dict = {
            "topic": _snap_topic(
                _slug_topic(sub_question, name),
                [kp.get("topic") for kp in _active(kb.get(name, []))
                 if isinstance(kp, dict) and kp.get("topic")]),
            "claim": findings,
            "numbers": [],
            "source_call": "",
            "confidence": "high",
        }
        turn_id = getattr(app_ctx, "_turn_id", None)
        if turn_id is not None:
            kp_dict["captured_at_turn"] = turn_id
        turn_seq = getattr(app_ctx, "_turn_seq", None)
        if turn_seq is not None:
            kp_dict["captured_at_seq"] = turn_seq
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

    # Existing slugs for THIS specialist, so a re-capture of a topic already in
    # the KB reuses its exact slug and supersedes it (rather than forking a
    # near-identical name that `_active_kps` treats as a separate topic). Only
    # the active set — superseded entries share their slug with the active one
    # by definition. Bounded so a long case can't crowd out the payload.
    from tools.kb_tools import _active_kps
    existing_topics = [kp.get("topic") for kp in _active_kps(kb.get(name, []))
                       if isinstance(kp, dict) and kp.get("topic")]
    # The PROMPT is truncated to the most recent slugs; `_snap_topic` below
    # still checks the full list — it's a pure set comparison, so there is no
    # reason to let an older topic drift just because it fell off the prompt.
    existing_block = (
        "\n--- Topic slugs already in this specialist's KB ---\n"
        + ", ".join(existing_topics[-_MAX_EXISTING_TOPICS:])
        + "\nIf a KP you emit answers the SAME question as one of these, reuse "
          "that slug EXACTLY (it supersedes the old entry). Only invent a new "
          "slug for a genuinely different question.\n"
    ) if existing_topics else ""

    distiller_input = (
        f"Specialist: {name}\n"
        f"Sub-question: {sub_question}\n"
        f"{existing_block}\n"
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
        # A truncated response is the DOMINANT distiller failure (26 of 38 in
        # the logs), and it used to cost the whole turn's knowledge. Salvage the
        # complete knowledge points out of the cut-off JSON before giving up —
        # losing the last KP is a far smaller loss than losing all of them.
        salvaged = _salvage_truncated_kps(exc)
        if logger is not None:
            logger.log("distiller_failed", {
                "specialist": name,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "salvaged_kps": len(salvaged),
            })
        if not salvaged:
            timer.summary(outcome="failed", error_type=type(exc).__name__)
            return 0
        timer.record("distiller_runner", int((time.perf_counter() - t0) * 1000))
        result = None
        new_kps = salvaged
    else:
        out = getattr(result, "final_output", None)
        new_kps = getattr(out, "knowledge_points", None) or []
    if not isinstance(new_kps, list):
        timer.summary(outcome="no_kps", n_added=0)
        return 0

    # Pre-parse series from tool outputs for post-fill (fills null values
    # the distiller left in `numbers` with real data from tool results).
    parsed_series = _parse_series_from_tool_outputs(tool_outputs)

    turn_id = getattr(app_ctx, "_turn_id", None)
    turn_seq = getattr(app_ctx, "_turn_seq", None)
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
        # Backstop for the prompt rule above: the instruction is advisory, this
        # is not. A token-permutation of an existing slug is snapped back onto
        # it so supersession actually fires.
        _emitted = kp_dict.get("topic") or ""
        _snapped = _snap_topic(_emitted, existing_topics)
        if _snapped != _emitted:
            kp_dict["topic"] = _snapped
            if logger is not None:
                logger.log("distiller_topic_snapped", {
                    "specialist": name, "emitted": _emitted,
                    "snapped_to": _snapped, "turn_id": turn_id,
                })

        if turn_id is not None and not kp_dict.get("captured_at_turn"):
            kp_dict["captured_at_turn"] = turn_id
        # Unconditional: `captured_at_seq` is ours to assign, not the
        # distiller's — a value hallucinated into the structured output would
        # corrupt the age ordering the compaction depends on.
        if turn_seq is not None:
            kp_dict["captured_at_seq"] = turn_seq

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
            # Also snap-able by later KPs in THIS batch, so a run that emits
            # two permutations of one topic collapses them too.
            existing_topics.append(kp_dict["topic"])

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
