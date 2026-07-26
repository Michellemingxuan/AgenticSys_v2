"""Async, defensive Amem read helpers. On disabled/empty/error they return the
empty result so callers fall back to today's warmth-hint + episodic behavior."""
from __future__ import annotations

import asyncio

from .config import AmemConfig
from .scope import build_scope

_LEVELS_CONTEXT = ["working", "conversation", "case"]
_LEVELS_KP = ["working", "conversation"]


async def retrieve_context(amem, cfg: AmemConfig, *, case_id: str, question: str) -> str:
    try:
        results = await asyncio.wait_for(
            amem.asearch_related(
                question,
                levels=_LEVELS_CONTEXT,
                scope=build_scope(cfg, case_id),
                search_mode="hybrid",
                limit=cfg.retrieve_limit,
                include_working=True,
            ),
            timeout=cfg.read_timeout_s,
        )
    except Exception:
        return ""
    if not results:
        return ""
    lines = []
    for r in results:
        rec = getattr(r, "record", None)
        if rec is None:
            continue
        level = getattr(getattr(rec, "level", None), "value", "memory")
        content = getattr(rec, "content", "") or ""
        if content:
            lines.append(f"  - [{level}] {content}")
    if not lines:
        return ""
    return (
        "[AMEM — relevant prior knowledge for this case (full claims, most "
        "relevant first). Use to avoid redundant queries and to anchor "
        "sub-questions:\n" + "\n".join(lines) + "\n]"
    )


async def search_kp(amem, cfg: AmemConfig, *, case_id: str, topic: str) -> str | None:
    try:
        results = await asyncio.wait_for(
            amem.asearch_related(
                topic,
                levels=_LEVELS_KP,
                scope=build_scope(cfg, case_id),
                search_mode="hybrid",
                limit=3,
                include_working=True,
            ),
            timeout=cfg.read_timeout_s,
        )
    except Exception:
        return None
    for r in results:
        rec = getattr(r, "record", None)
        content = (getattr(rec, "content", "") or "") if rec else ""
        if content:
            return content
    return None
