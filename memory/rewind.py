"""Sync Amem delete-by-turn. Called from Flask rewind/cancel handlers. Deletes by
turn_id (a real scope field) — Amem cannot filter by session metadata."""
from __future__ import annotations

from .config import AmemConfig
from .scope import build_scope


def delete_turns(amem, cfg: AmemConfig, *, case_id: str, turn_ids) -> int:
    deleted = 0
    for turn_id in turn_ids or []:
        try:
            records = amem.list_memories(
                scope=build_scope(cfg, case_id, turn_id=turn_id),
                include_working=True,
            )
        except Exception:
            continue
        for rec in records:
            try:
                if amem.delete_memory(rec.id):
                    deleted += 1
            except Exception:
                continue
    return deleted


def delete_case_memory(amem, cfg: AmemConfig, *, case_id: str) -> int:
    """Purge ALL Amem memory for a case (working/conversation/case, every turn).
    Used by Clear History. Never raises."""
    deleted = 0
    try:
        records = amem.list_memories(
            scope=build_scope(cfg, case_id),   # case-only: no turn/agent filter
            include_working=True,
        )
    except Exception:
        return 0
    for rec in records:
        try:
            if amem.delete_memory(rec.id):
                deleted += 1
        except Exception:
            continue
    return deleted
