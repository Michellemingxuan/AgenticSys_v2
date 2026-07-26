from __future__ import annotations

from Amem import MemoryScope

from .config import AmemConfig


def build_scope(cfg: AmemConfig, case_id: str, *,
                turn_id: str | None = None,
                agent_id: str | None = None) -> MemoryScope:
    return MemoryScope(
        org_id=cfg.org_id,
        user_id=cfg.user_id,
        case_id=case_id,
        turn_id=turn_id,
        agent_id=agent_id,
    )


def base_metadata(session_id: str | None) -> dict:
    return {"session_id": session_id} if session_id else {}


def kps_for_turn(specialist_kb: dict, turn_id: str) -> list[str]:
    out: list[str] = []
    for kps in (specialist_kb or {}).values():
        for kp in kps or []:
            if kp.get("captured_at_turn") != turn_id:
                continue
            claim = (kp.get("claim") or "").strip()
            if claim:
                out.append(claim)
    return out


def kps_for_agent_turn(specialist_kb: dict, agent_id: str, turn_id: str) -> list[dict]:
    """Full KP dicts for ONE agent captured on ONE turn — the slice
    `write_specialist_memory` embeds in that specialist's durable record."""
    out: list[dict] = []
    for kp in (specialist_kb or {}).get(agent_id, []) or []:
        if kp.get("captured_at_turn") == turn_id:
            out.append(kp)
    return out
