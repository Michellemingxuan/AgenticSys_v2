"""Batched load of the RAM specialist_kb dict from Amem (the durable source of
truth), at turn start. One case-scoped query — no per-step DB round-trips, no
dependency on Amem semantic search (metadata/case_id filter only).

Forward-compatible: to move to relevance-ranked loading later, swap the
`list_memories` call for `asearch_related(query, scope=case, search_mode="hybrid")`
— the records are embedded and the structured KPs live in metadata, so
reconstruction is identical regardless of how records are selected.
"""
from __future__ import annotations

from .config import AmemConfig
from .scope import build_scope


def load_case_kps(amem, cfg: AmemConfig, *, case_id: str) -> dict:
    """Return `{agent_id: [kp_dict, ...]}` reconstructed from the case's durable
    per-specialist conversation records. Chronological (oldest-first) so the
    existing "latest KP per topic = active" logic in kb_tools holds. The
    orchestrator turn record is skipped (it carries no per-specialist KP set).
    Never raises → `{}` on error/empty."""
    try:
        records = amem.list_memories(
            scope=build_scope(cfg, case_id),
            levels=["conversation"],
        )
    except Exception:
        return {}

    def _created(r):
        return getattr(r, "created_at", "") or ""

    try:
        records = sorted(records or [], key=_created)
    except Exception:
        records = list(records or [])

    out: dict[str, list] = {}
    for r in records:
        agent = getattr(getattr(r, "scope", None), "agent_id", None)
        if not agent or agent == "orchestrator":
            continue
        meta = getattr(r, "metadata", None) or {}
        kps = meta.get("knowledge_points")
        if not kps:
            continue
        out.setdefault(agent, []).extend(kps)
    return out
