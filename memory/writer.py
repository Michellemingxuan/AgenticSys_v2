# memory/writer.py
"""Async, defensive Amem write helpers. Every function swallows exceptions and
returns None — an Amem write failure OR malformed input must never break a turn."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .config import AmemConfig
from .scope import base_metadata, build_scope


async def _guard(make_awaitable: Callable[[], Awaitable[Any]], timeout: float) -> Any:
    """Await make_awaitable() under a timeout, swallowing ALL exceptions —
    including any raised while BUILDING the awaitable (scope/metadata
    construction happens inside the coroutine body, thus inside this try)."""
    try:
        return await asyncio.wait_for(make_awaitable(), timeout=timeout)
    except Exception:
        return None


async def mirror_kp_working(amem, cfg: AmemConfig, kp_dict: dict, *,
                            case_id: str, turn_id: str, agent_id: str,
                            session_id: str) -> None:
    async def _do():
        metadata = base_metadata(session_id)
        metadata.update({
            "topic": kp_dict.get("topic"),
            "numbers": kp_dict.get("numbers"),
            "confidence": kp_dict.get("confidence"),
            "captured_at_turn": kp_dict.get("captured_at_turn"),
        })
        return await amem.aadd_memory(
            level="working",
            content=(kp_dict.get("claim") or ""),
            scope=build_scope(cfg, case_id, turn_id=turn_id, agent_id=agent_id),
            kind="knowledge_point",
            metadata=metadata,
        )
    await _guard(_do, cfg.write_timeout_s)


async def write_conversation(amem, cfg: AmemConfig, *, question: str, answer: str,
                             case_id: str, turn_id: str, session_id: str,
                             atomic_facts: list[str]) -> None:
    async def _do():
        return await amem.arecord_conversation(
            raw_question=question,
            raw_answer=answer,
            scope=build_scope(cfg, case_id, turn_id=turn_id, agent_id="orchestrator"),
            atomic_facts=(atomic_facts or None),
            metadata=base_metadata(session_id),
        )
    await _guard(_do, cfg.write_timeout_s)


async def consolidate_case(amem, cfg: AmemConfig, *, case_id: str,
                           session_id: str) -> None:
    async def _do():
        return await amem.aupsert_case_memory(
            scope=build_scope(cfg, case_id),
            metadata=base_metadata(session_id),
        )
    await _guard(_do, cfg.write_timeout_s)


async def write_specialist_memory(amem, cfg: AmemConfig, *, case_id: str, turn_id: str,
                                  session_id: str, agent_id: str, sub_question: str,
                                  findings: str, kps: list | None,
                                  tool_calls: list | None) -> None:
    """Durable per-specialist conversation record: the specialist's sub-Q/A, its KP
    claims as atomic_facts, and — in metadata — the FULL structured KPs (for faithful
    reload via load_case_kps) plus the tool calls it made (func + params, no payloads).
    Best-effort; never raises."""
    async def _do():
        claims = [
            (kp.get("claim") or "").strip()
            for kp in (kps or [])
            if isinstance(kp, dict) and (kp.get("claim") or "").strip()
        ]
        metadata = base_metadata(session_id)
        metadata.update({
            "knowledge_points": list(kps or []),
            "tool_calls": list(tool_calls or []),
        })
        return await amem.arecord_conversation(
            raw_question=(sub_question or ""),
            raw_answer=(findings or ""),
            scope=build_scope(cfg, case_id, turn_id=turn_id, agent_id=agent_id),
            atomic_facts=(claims or None),
            metadata=metadata,
        )
    await _guard(_do, cfg.write_timeout_s)
