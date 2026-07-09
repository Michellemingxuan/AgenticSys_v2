"""Layer-1 prior-turn recall: Q->A exact-match cache + KnowledgePoint lookup."""
from __future__ import annotations

import os

# Cap on the per-session Q->A cache (see `_store_cached_qa` below). Moved
# here from `server.py` alongside the function that is its only consumer —
# it isn't a shared cross-module constant, so it doesn't belong in
# `runner/config.py`.
_QA_CACHE_MAX_ENTRIES = int(os.environ.get("QA_CACHE_MAX_ENTRIES", "64"))


def _normalize_q(q: str) -> str:
    """Normalize a question for the per-session exact-match QA cache.

    Lowercase, strip outer whitespace, collapse internal whitespace. The
    cache is intentionally exact-match-after-redaction (run_normalize on
    `verdict.redacted_question`); fuzzy similarity is the orchestrator's
    job (team_construction.md), not the cache's.
    """
    return " ".join((q or "").strip().lower().split())


def _get_cached_qa(sess, cache_key: str | None) -> dict | None:
    """Return a QA-cache entry and refresh its insertion order.

    ``dict`` preserves insertion order on supported Python versions, so a
    pop/reinsert gives us LRU behavior without changing the stored type or
    existing tests/fixtures.
    """
    if not cache_key:
        return None
    cached = sess.qa_cache.get(cache_key)
    if cached is None:
        return None
    try:
        sess.qa_cache[cache_key] = sess.qa_cache.pop(cache_key)
    except Exception:
        pass
    return cached


def _store_cached_qa(sess, cache_key: str | None, value: dict) -> int:
    """Store a QA-cache entry and evict oldest entries beyond the cap.

    Returns the number of entries evicted. The cache is a speed optimization,
    not the audit source, so bounding it avoids long sessions retaining every
    answer payload forever.
    """
    if not cache_key:
        return 0
    sess._qa_turn_seq += 1
    value["turn_seq"] = sess._qa_turn_seq
    sess.qa_cache[cache_key] = value
    evicted = 0
    while _QA_CACHE_MAX_ENTRIES > 0 and len(sess.qa_cache) > _QA_CACHE_MAX_ENTRIES:
        try:
            oldest = next(iter(sess.qa_cache))
        except StopIteration:
            break
        sess.qa_cache.pop(oldest, None)
        evicted += 1
    return evicted


def _find_kp(specialist_kb: dict, specialist: str, topic: str,
             turn_id: str) -> dict | None:
    """Return the latest KP for (specialist, topic) captured in this turn,
    or None when not present. Used to enrich the chart SSE event with the
    KP's claim / source_call / vega_spec."""
    if not isinstance(specialist_kb, dict):
        return None
    kps = specialist_kb.get(specialist) or []
    found: dict | None = None
    for kp in kps:
        if not isinstance(kp, dict):
            continue
        if kp.get("captured_at_turn") != turn_id:
            continue
        if kp.get("topic") != topic:
            continue
        found = kp  # latest-wins (chronological iteration)
    return found
