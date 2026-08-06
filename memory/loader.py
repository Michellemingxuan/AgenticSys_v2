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


def kp_seq(kp: dict) -> int:
    """Sortable age of a KP: its `captured_at_seq` stamp, or -1 when absent.

    -1 (= oldest) covers KPs written before the field existed, matching the
    `e.get("turn_seq", -1)` convention in `tools/episodic.py`."""
    try:
        value = kp.get("captured_at_seq")
        return int(value) if value is not None else -1
    except (AttributeError, TypeError, ValueError):
        return -1


def max_kp_seq(kb: dict) -> int:
    """Highest `captured_at_seq` across a `{agent: [kp, ...]}` KB, or -1."""
    best = -1
    for kps in (kb or {}).values():
        for kp in kps or []:
            if isinstance(kp, dict):
                best = max(best, kp_seq(kp))
    return best


def _identity(agent: str, kp: dict) -> tuple:
    """Dedup key for a KP. `topic` alone is not enough — the same topic is
    legitimately re-captured across turns (that IS the supersession trail) —
    so the turn that produced it is part of the identity."""
    return (agent, kp.get("topic"), kp.get("captured_at_turn"), kp_seq(kp))


def merge_recent_kps(compacted: dict, previous: dict, *, keep: int = ACTIVE_KP_KEEP) -> dict:
    """Union a relevance-compacted KB with the newest `keep` KPs of the
    pre-compaction working set.

    Amem's hybrid search ranks on embedding + keyword similarity ONLY — there
    is no recency term (`_rank_matches` in Amem/core/manager.py). So a plain
    replacement can drop the KPs the last turn just produced, which is exactly
    what a follow-up ("think harder", "what contradicts that?") needs and what
    carries the least lexical signal toward the new question. Pinning the
    newest `keep` by `captured_at_seq` makes that impossible.

    The result is bounded at ~2*keep. Recent KPs already present in `compacted`
    (they ARE in Amem by now, so the search can legitimately return them) are
    deduped rather than doubled. Each agent's list comes out ordered oldest →
    newest so `_active_kps`'s latest-wins supersession still holds.
    """
    out: dict[str, list] = {a: list(kps or []) for a, kps in (compacted or {}).items()}
    flat: list[tuple[int, int, str, dict]] = []
    for agent, kps in (previous or {}).items():
        for idx, kp in enumerate(kps or []):
            if isinstance(kp, dict):
                flat.append((kp_seq(kp), idx, agent, kp))
    if not flat or keep <= 0:
        return out
    # Ties (same seq, e.g. several KPs from one turn) keep their original
    # in-list order — `idx` is the tiebreak, and the sort is a stable ascending
    # one whose tail is the newest `keep`.
    flat.sort(key=lambda item: (item[0], item[1]))
    seen = {_identity(a, kp) for a, kps in out.items()
            for kp in kps if isinstance(kp, dict)}
    for _seq, _idx, agent, kp in flat[-keep:]:
        ident = _identity(agent, kp)
        if ident in seen:
            continue
        seen.add(ident)
        out.setdefault(agent, []).append(kp)
    for kps in out.values():
        kps.sort(key=kp_seq)   # stable: preserves insertion order within a turn
    return out


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

    # SELECT by relevance, ORDER by time. The search returns records ranked by
    # similarity; taking KPs in that order and stopping at `limit` is correct
    # for *which* KPs survive, but it scrambles their chronology — and
    # `_active_kps` resolves a repeated topic by taking the LAST entry in the
    # list. Rebuilding in age order keeps latest-wins meaning latest, so a
    # compaction can't resurrect a superseded claim into the digest.
    selected: list[tuple[int, str, str, dict]] = []
    count = 0
    for r in results or []:
        rec = getattr(r, "record", None)
        if rec is None:
            continue
        agent = getattr(getattr(rec, "scope", None), "agent_id", None)
        if not agent or agent == "orchestrator":
            continue
        created = str(getattr(rec, "created_at", "") or "")
        kps = (getattr(rec, "metadata", None) or {}).get("knowledge_points") or []
        for kp in kps:
            if count >= limit:
                break
            selected.append((kp_seq(kp), created, agent, kp))
            count += 1
        if count >= limit:
            break
    # `created_at` breaks ties for legacy KPs that predate `captured_at_seq`
    # (all seq -1) — within one record it degrades to arrival order.
    selected.sort(key=lambda item: (item[0], item[1]))
    out: dict[str, list] = {}
    for _seq, _created, agent, kp in selected:
        out.setdefault(agent, []).append(kp)
    return out
