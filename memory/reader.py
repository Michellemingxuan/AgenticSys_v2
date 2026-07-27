"""Sync Amem read helper: the durable case summary, injected as the condensed
"older context" for the orchestrator once a case runs past the episodic window.

Read-only and defensive — returns "" on disabled/empty/error so the caller
simply omits the block. (The old semantic-read helpers retrieve_context /
search_kp were removed: relevance-scoping now happens once at load time via
load_active_kps, and kb_lookup is a pure RAM cache — see the memory design.)"""
from __future__ import annotations

from .config import AmemConfig
from .scope import build_scope


def load_case_summary(amem, cfg: AmemConfig, *, case_id: str) -> str:
    """Return the durable Amem case-summary content for this case, or "".

    Same source as build_session_brief (aupsert_case_memory stores it as
    level='case', kind='case_summary'), but returns "" — not a welcome line —
    when absent, so the caller can cleanly skip the summary block."""
    try:
        records = amem.list_memories(
            levels=["case"],
            scope=build_scope(cfg, case_id),
            kind="case_summary",
            limit=1,
        )
    except Exception:
        return ""
    for rec in records or []:
        content = (getattr(rec, "content", "") or "").strip()
        if content:
            return content
    return ""
