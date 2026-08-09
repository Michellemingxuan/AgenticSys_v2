# memory/writer.py
"""Async, defensive Amem write helpers. Every function swallows exceptions and
returns None — an Amem write failure OR malformed input must never break a turn."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .config import AmemConfig
from .scope import base_metadata, build_scope


def _log(logger, event: str, payload: dict) -> None:
    """Logging must never be what breaks the write it is reporting on."""
    if logger is None:
        return
    try:
        logger.log(event, payload)
    except Exception:  # noqa: BLE001
        pass


async def _guard(make_awaitable: Callable[[], Awaitable[Any]], timeout: float,
                 *, logger=None, op: str = "") -> Any:
    """Await make_awaitable() under a timeout, swallowing ALL exceptions —
    including any raised while BUILDING the awaitable (scope/metadata
    construction happens inside the coroutine body, thus inside this try).

    SWALLOWED IS NOT SILENT. Never breaking a turn is the right behaviour; being
    undiagnosable is not. With no logging here, "memory is not being stored"
    looks identical to "memory is being stored fine" from the outside — every
    write can fail, forever, and the only visible symptom is an empty store.
    Reported from the private env, where nothing in the logs said why.

    TIMEOUT IS ITS OWN OUTCOME, and the likeliest one in prod. The default
    budget is 5s (`AMEM_WRITE_TIMEOUT_S`), while `aupsert_case_memory` runs an
    LLM SUMMARIZATION inside Amem — fast on dev's OpenAI, not on safechain,
    where concurrent calls are documented as intermittently ~4x slower under
    Azure throttling. So it is logged distinctly from an error: a timeout means
    raise the budget, an error means fix the wiring.
    """
    try:
        return await asyncio.wait_for(make_awaitable(), timeout=timeout)
    except asyncio.TimeoutError:
        _log(logger, "amem_write_timeout", {"op": op, "timeout_s": timeout})
        return None
    except Exception as exc:  # noqa: BLE001 — a write must never break a turn
        _log(logger, "amem_write_failed", {
            "op": op, "error_type": type(exc).__name__, "error": str(exc)[:300],
        })
        return None


async def write_conversation(amem, cfg: AmemConfig, *, question: str, answer: str,
                             case_id: str, turn_id: str, session_id: str,
                             atomic_facts: list[str] | None = None,
                             team_dispatch: list[dict] | None = None, logger=None) -> None:
    """Durable orchestrator record for a turn: question + team dispatch
    (round 1: which specialists were called with what sub-question + concepts,
    in metadata) + final answer (round 2, as raw_answer). The specialists'
    distilled KPs live on their own per-specialist records — NOT here."""
    async def _do():
        metadata = base_metadata(session_id)
        if team_dispatch:
            metadata["team_dispatch"] = list(team_dispatch)
        # Pass [] (not None) when there are no facts: Amem treats
        # atomic_facts=None as "auto-summarize this Q&A into facts" (an extra
        # synthesis step that also embeds an "Atomic facts:" section into the
        # record content). The orchestrator record should stay clean —
        # question + team dispatch + final answer only.
        return await amem.arecord_conversation(
            raw_question=question,
            raw_answer=answer,
            scope=build_scope(cfg, case_id, turn_id=turn_id, agent_id="orchestrator"),
            atomic_facts=(atomic_facts or []),
            metadata=metadata,
        )
    await _guard(_do, cfg.write_timeout_s, logger=logger, op="write_conversation")


async def consolidate_case(amem, cfg: AmemConfig, *, case_id: str,
                           session_id: str, logger=None) -> None:
    async def _do():
        return await amem.aupsert_case_memory(
            scope=build_scope(cfg, case_id),
            metadata=base_metadata(session_id),
        )
    await _guard(_do, cfg.write_timeout_s, logger=logger, op="consolidate_case")


async def consolidate_agent_case(amem, cfg: AmemConfig, *, case_id: str,
                                 agent_id: str, session_id: str,
                                 min_turns: int, logger=None) -> None:
    """Per-specialist case summary: a condensed overview of ONE agent's
    accumulated findings across the case, stored as kind="agent_case_summary"
    scoped to (case, agent) so it never collides with the whole-case summary.

    Built only when the agent has run in more than `min_turns` turns (else its
    own recent-turn episodic already covers everything it knows). The turn count
    is the agent's durable conversation-record count (one per turn it ran) — a
    cheap list_memories filter, no embedding. Best-effort; never raises."""
    async def _do():
        scope = build_scope(cfg, case_id, agent_id=agent_id)
        try:
            n_turns = len(amem.list_memories(scope=scope, levels=["conversation"]))
        except Exception:
            n_turns = 0
        if n_turns <= min_turns:
            return None
        return await amem.aupsert_case_memory(
            scope=scope,
            kind="agent_case_summary",
            metadata=base_metadata(session_id),
        )
    await _guard(_do, cfg.write_timeout_s, logger=logger, op="consolidate_agent_case")


async def write_specialist_memory(amem, cfg: AmemConfig, *, case_id: str, turn_id: str,
                                  session_id: str, agent_id: str, sub_question: str,
                                  findings: str, kps: list | None,
                                  tool_calls: list | None, logger=None) -> None:
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
    await _guard(_do, cfg.write_timeout_s, logger=logger, op="write_specialist_memory")
