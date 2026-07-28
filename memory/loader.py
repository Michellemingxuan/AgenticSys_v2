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

# One knob for the whole behavior: once a case's RAM specialist_kb would exceed
# this many KPs, stop loading the WHOLE KB each turn and instead load the N most
# relevant KPs for the current question (load_active_kps) — so it is BOTH the
# switch point AND the subset size cap. Below it, the complete load is used.
# Env-tunable (AMEM_ACTIVE_KP_THRESHOLD) — see config/tuning.yaml. Read at import,
# so the tuning YAML must be applied before this module is imported (server.py
# does this at startup).
ACTIVE_KP_THRESHOLD = int(os.environ.get("AMEM_ACTIVE_KP_THRESHOLD", "100"))
_ACTIVE_LOAD_TIMEOUT_S = 6.0    # generous: this is a batch load, only for large cases


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
                          limit: int = ACTIVE_KP_THRESHOLD) -> dict:
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
