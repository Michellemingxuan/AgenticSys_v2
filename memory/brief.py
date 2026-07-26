"""Sync session-start brief: the case summary if one exists, else a welcome line."""
from __future__ import annotations

from .config import AmemConfig
from .scope import build_scope


def build_session_brief(amem, cfg: AmemConfig, *, case_id: str) -> str:
    try:
        records = amem.list_memories(
            levels=["case"],
            scope=build_scope(cfg, case_id),
            kind="case_summary",   # aupsert_case_memory stores case memory as kind="case_summary"
            limit=1,
        )
    except Exception:
        records = []
    for rec in records:
        content = (getattr(rec, "content", "") or "").strip()
        if content:
            return content
    return f"Welcome to the discovery journey of case {case_id}."
