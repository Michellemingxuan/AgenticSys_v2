"""Knowledge Base tools — let specialists look up cached data points
from previous queries instead of re-running expensive tool calls.

Two tools:
  - kb_list_topics() — browse cached topics (short list, no data)
  - kb_lookup(topic) — retrieve a specific cached data point with numbers
"""
from __future__ import annotations

import json
from typing import Any

from agents import RunContextWrapper, function_tool


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


def _format_kb_digest(kps: list[dict], full_kb: dict | None = None,
                      self_name: str | None = None) -> str:
    """Render a short KB hint pointing to the lookup tools.

    Instead of dumping all KP claims into the input (which inflates token
    count on follow-up turns), we list topic names only and tell the
    specialist to use kb_lookup(topic) for details.

    When *full_kb* and *self_name* are provided, also lists topic counts
    from OTHER specialists so this specialist knows cross-domain data is
    available via kb_lookup / kb_list_topics without re-querying.
    """
    active = _active_kps(kps)
    parts: list[str] = []
    if active:
        topics = [kp.get("topic", "?") for kp in active]
        parts.append(
            f"[KB — your cached topics ({len(active)}): "
            f"{', '.join(topics)}.]"
        )
    other_lines: list[str] = []
    if full_kb and self_name:
        for spec, spec_kps in sorted(full_kb.items()):
            if spec == self_name:
                continue
            other_active = _active_kps(spec_kps)
            if other_active:
                other_topics = [kp.get("topic", "?") for kp in other_active]
                other_lines.append(f"  {spec}: {', '.join(other_topics)}")
    if other_lines:
        parts.append(
            "[KB — other specialists' cached topics "
            "(use kb_lookup(topic) to retrieve without re-querying):\n"
            + "\n".join(other_lines) + "]"
        )
    if not parts:
        return ""
    parts.append(
        "Call kb_lookup(topic) to get cached data before re-querying. "
        "Call kb_list_topics() to see all cached claims."
    )
    return "\n".join(parts)


@function_tool
async def kb_list_topics(ctx: RunContextWrapper) -> str:
    """List all cached topics in the knowledge base from previous turns.
    Returns topic names + one-line claims. Use kb_lookup(topic) to get
    the full data including numbers."""
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
    Use this BEFORE re-running an expensive summarize_trend call — if the
    EXACT metric the question asks is cached, you can skip the query.
    A cached topic is an answer ONLY when it is the same metric / entity /
    window being asked. A near-miss (e.g. a cached card-count when the
    question asks for balance) is NOT an answer — query the real data and
    never fabricate the asked number from an unrelated cached value."""
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
