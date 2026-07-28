"""Batched load of the RAM specialist_kb dict from Amem (the durable source of
truth), at turn start. One case-scoped query — no per-step DB round-trips, no
dependency on Amem semantic search (metadata/case_id filter only).

Forward-compatible: to move to relevance-ranked loading later, swap the
`list_memories` call for `asearch_related(query, scope=case, search_mode="hybrid")`
— the records are embedded and the structured KPs live in metadata, so
reconstruction is identical regardless of how records are selected.
"""
from __future__ import annotations

import asyncio
import os

from .config import AmemConfig
from .scope import build_scope

# Accumulate-then-compact working-set control. specialist_kb accumulates KPs in
# RAM across turns; only when it crosses ACTIVE_KP_THRESHOLD (K1) do we pay one
# hybrid search to compact it to the ACTIVE_KP_KEEP (K2 << K1) most-relevant KPs,
# then it accumulates again. This hysteresis makes the expensive Amem search
# fire ~every (K1-K2)/kps_per_turn turns instead of every turn.
# Both env-tunable (see config/tuning.yaml), read at import — the tuning YAML
# must be applied before this module is imported (server.py does this at startup).
ACTIVE_KP_THRESHOLD = int(os.environ.get("AMEM_ACTIVE_KP_THRESHOLD", "100"))  # K1: compact trigger
ACTIVE_KP_KEEP = int(os.environ.get("AMEM_ACTIVE_KP_KEEP", "20"))             # K2: compact target
_ACTIVE_LOAD_TIMEOUT_S = 6.0    # generous: this batch search only fires at compaction


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


async def load_active_kps(amem, cfg: AmemConfig, *, case_id: str, question: str,
                          limit: int = ACTIVE_KP_KEEP) -> dict:
    """Relevance-scoped subset of a case's KPs, for KBs too large to hold whole.

    Retrieves the most relevant conversation records for *question* via Amem
    hybrid search and reconstructs `{agent_id: [kp, ...]}` from their
    `knowledge_points`, capped at *limit* KPs total (relevance order preserved).
    Skips the orchestrator record. At scale this subset IS the specialist's
    cached world for the turn: a KP left out of it is not browsable/lookup-able
    from RAM, and the specialist re-queries the real data (ground truth) if it
    needs something outside the subset.

    Never raises → returns `{}` on timeout / error / empty (the caller then
    keeps the full load_case_kps result, so this only ever helps)."""
    try:
        results = await asyncio.wait_for(
            amem.asearch_related(
                question,
                levels=["conversation"],
                scope=build_scope(cfg, case_id),
                search_mode="hybrid",
                # Fetch more RECORDS than the KP cap: each record carries several
                # KPs and some (orchestrator) carry none, so a record-count == KP
                # cap would under-fill the subset. KPs are still capped at `limit`.
                limit=max(limit * 3, 20),
                include_working=False,
            ),
            timeout=_ACTIVE_LOAD_TIMEOUT_S,
        )
    except Exception:
        return {}

    out: dict[str, list] = {}
    count = 0
    for r in results or []:
        rec = getattr(r, "record", None)
        if rec is None:
            continue
        agent = getattr(getattr(rec, "scope", None), "agent_id", None)
        if not agent or agent == "orchestrator":
            continue
        kps = (getattr(rec, "metadata", None) or {}).get("knowledge_points") or []
        for kp in kps:
            if count >= limit:
                return out
            out.setdefault(agent, []).append(kp)
            count += 1
    return out
