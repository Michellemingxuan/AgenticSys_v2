"""Knowledge Base tools — let specialists look up cached data points
from previous queries instead of re-running expensive tool calls.

Two tools:
  - kb_list_topics() — browse cached topics (short list, no data)
  - kb_lookup(topic) — retrieve a specific cached data point with numbers
"""
from __future__ import annotations

import json
import os
from typing import Any

from agents import RunContextWrapper, function_tool


# ── Ablation switch: cross-turn specialist memory ───────────────────────────
#
# THIS IS THE BASELINE BUILD, so the default here is OFF — the distiller's
# second pass does not run, no KnowledgePoints are extracted, and specialists
# start every turn cold. Set `DISTILLER_ENABLED=1` to restore the accumulating
# behaviour for an A/B run without editing code.
#
# "Off" has to mean off on all three surfaces, or the ablation silently leaks:
#   1. the distiller task itself (`redacting_tool` — the LLM pass + the
#      narrow-output direct insert),
#   2. the `[KB-warmth]` preface the orchestrator reads (`server.py`),
#   3. the `kb_lookup` / `kb_list_topics` tools below.
#
# (2) is the subtle one. The AUTO-CHART path writes its own KPs into the SAME
# `specialist_kb` — that is how a rendered chart reaches `_collect_turn_charts`
# — and those KPs carry a full `claim` string ("2024-01 to 2025-06: FICO Score:
# 620 -> 710 (peak 715…)"). So disabling only the distiller would still leave
# the warmth hint populated from chart claims, and the baseline would quietly
# keep a cross-turn memory through the chart side-channel. Charts must keep
# writing to the KB; the READ paths are what get gated.
DISTILLER_ENABLED = os.environ.get(
    "DISTILLER_ENABLED", "0"
).strip().lower() not in {"0", "false", "no", "off", ""}

_KB_DISABLED_MSG = (
    "Knowledge base is DISABLED for this run (baseline configuration): no "
    "knowledge points are retained between turns. This is expected — do not "
    "retry, and do not report it as a data gap. Query the tables directly."
)


def _get_kb(ctx: RunContextWrapper) -> dict[str, list] | None:
    app_ctx = ctx.context if ctx else None
    return getattr(app_ctx, "_specialist_kb", None)


def _active_kps(kps: list[dict]) -> list[dict]:
    """Latest KP per topic (same logic as redacting_tool._active_kps)."""
    active: dict[str, dict] = {}
    for kp in kps or []:
        topic = kp.get("topic")
        if topic:
            active[topic] = kp
    return list(active.values())


@function_tool
async def kb_list_topics(ctx: RunContextWrapper) -> str:
    """List all cached topics in the knowledge base from previous turns.
    Returns topic names + one-line claims. Use kb_lookup(topic) to get
    the full data including numbers."""
    if not DISTILLER_ENABLED:
        return _KB_DISABLED_MSG
    kb = _get_kb(ctx)
    if not kb:
        return "KB is empty."
    lines = []
    for spec_name, kps in sorted(kb.items()):
        active = _active_kps(kps)
        for kp in active:
            topic = kp.get("topic", "?")
            claim = (kp.get("claim") or "")[:100]
            conf = kp.get("confidence", "medium")
            has_numbers = bool(kp.get("numbers"))
            lines.append(
                f"- {topic} [{spec_name}, {conf}]"
                f"{' (has numbers)' if has_numbers else ''}: {claim}"
            )
    return "\n".join(lines) if lines else "KB is empty."


@function_tool
async def kb_lookup(ctx: RunContextWrapper, topic: str) -> str:
    """Look up a specific cached knowledge point by topic slug.
    Returns the full claim + numbers + source_call if found.
    Use this BEFORE re-running an expensive summarize_trend call —
    if the topic is cached, you can skip the query."""
    if not DISTILLER_ENABLED:
        return _KB_DISABLED_MSG
    kb = _get_kb(ctx)
    if not kb:
        return f"Topic '{topic}' not found — KB is empty."
    topic_lower = topic.lower().strip()
    for spec_name, kps in kb.items():
        for kp in reversed(kps):  # latest first
            kp_topic = (kp.get("topic") or "").lower().strip()
            if kp_topic == topic_lower:
                result: dict[str, Any] = {
                    "topic": kp.get("topic"),
                    "specialist": spec_name,
                    "claim": kp.get("claim"),
                    "confidence": kp.get("confidence"),
                    "source_call": kp.get("source_call"),
                }
                numbers = kp.get("numbers")
                if numbers:
                    result["numbers"] = numbers
                return json.dumps(result, default=str)
    return f"Topic '{topic}' not found in KB."
