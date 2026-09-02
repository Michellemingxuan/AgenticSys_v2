"""Knowledge-point (KP) cache tools — let specialists look up data points
distilled from previous turns instead of re-running expensive tool calls.

This is the SESSION-LOCAL cache of what specialists already found. It is
unrelated to the internal clustered case-report KNOWLEDGE BASE, which is
reached through `tools/knowledge_base.py` — keep the two terms apart.

Two tools:
  - kp_list_topics() — browse cached topics (short list, no data)
  - kp_lookup(topic) — retrieve a specific cached data point with numbers
"""
from __future__ import annotations

import json
from typing import Any

from agents import RunContextWrapper, function_tool


def _get_kps(ctx: RunContextWrapper) -> dict[str, list] | None:
    app_ctx = ctx.context if ctx else None
    return getattr(app_ctx, "_specialist_kps", None)


def _active_kps(kps: list[dict]) -> list[dict]:
    """Latest KP per topic. The KP list is appended chronologically, so
    iterating in order and keeping the last-seen entry per topic gives the
    active set. Single source of truth (imported by agent_tool._runner)."""
    active: dict[str, dict] = {}
    for kp in kps or []:
        topic = kp.get("topic")
        if topic:
            active[topic] = kp
    return list(active.values())


def _format_kp_digest(kps: list[dict], full_kps: dict | None = None,
                      self_name: str | None = None) -> str:
    """Render a short KP hint pointing to the lookup tools.

    Instead of dumping all KP claims into the input (which inflates token
    count on follow-up turns), we list topic names only and tell the
    specialist to use kp_lookup(topic) for details.

    When *full_kps* and *self_name* are provided, also lists topic counts
    from OTHER specialists so this specialist knows cross-domain data is
    available via kp_lookup / kp_list_topics without re-querying.
    """
    active = _active_kps(kps)
    parts: list[str] = []
    if active:
        topics = [kp.get("topic", "?") for kp in active]
        parts.append(
            f"[KP — your cached topics ({len(active)}): "
            f"{', '.join(topics)}.]"
        )
    other_lines: list[str] = []
    if full_kps and self_name:
        for spec, spec_kps in sorted(full_kps.items()):
            if spec == self_name:
                continue
            other_active = _active_kps(spec_kps)
            if other_active:
                other_topics = [kp.get("topic", "?") for kp in other_active]
                other_lines.append(f"  {spec}: {', '.join(other_topics)}")
    if other_lines:
        parts.append(
            "[KP — other specialists' cached topics "
            "(use kp_lookup(topic) to retrieve without re-querying):\n"
            + "\n".join(other_lines) + "]"
        )
    if not parts:
        return ""
    parts.append(
        "Call kp_lookup(topic) to get cached data before re-querying. "
        "Call kp_list_topics() to see all cached claims."
    )
    return "\n".join(parts)


@function_tool
async def kp_list_topics(ctx: RunContextWrapper) -> str:
    """List all cached topics in the knowledge base from previous turns.
    Returns topic names + one-line claims. Use kp_lookup(topic) to get
    the full data including numbers."""
    store = _get_kps(ctx)
    if not store:
        return "No knowledge points cached yet."
    lines = []
    for spec_name, spec_kps in sorted(store.items()):
        active = _active_kps(spec_kps)
        for kp in active:
            topic = kp.get("topic", "?")
            claim = (kp.get("claim") or "")[:100]
            conf = kp.get("confidence", "medium")
            has_numbers = bool(kp.get("numbers"))
            lines.append(
                f"- {topic} [{spec_name}, {conf}]"
                f"{' (has numbers)' if has_numbers else ''}: {claim}"
            )
    return "\n".join(lines) if lines else "No knowledge points cached yet."


@function_tool
async def kp_lookup(ctx: RunContextWrapper, topic: str) -> str:
    """Look up a specific cached knowledge point by topic slug.
    Returns the full claim + numbers + source_call if found.
    Use this BEFORE re-running an expensive summarize_trend call — if the
    EXACT metric the question asks is cached, you can skip the query.
    A cached topic is an answer ONLY when it is the same metric / entity /
    window being asked. A near-miss (e.g. a cached card-count when the
    question asks for balance) is NOT an answer — query the real data and
    never fabricate the asked number from an unrelated cached value."""
    store = _get_kps(ctx)
    topic_lower = topic.lower().strip()
    if store:
        for spec_name, spec_kps in store.items():
            for kp in reversed(spec_kps):  # latest first
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

    # RAM-only cache: a miss means "not cached in the active working set" — the
    # specialist should query the real data (ground truth). No Amem fallback:
    # relevance-scoping happens once at load time (load_active_kps), so the
    # working set already holds the KPs relevant to this turn.
    if not store:
        return (f"Topic '{topic}' not found — no knowledge points cached yet; "
                f"query the data directly.")
    return (f"Topic '{topic}' not in the cached working set — query the data "
            f"directly (the KP cache only holds distilled prior findings for "
            f"this turn).")
