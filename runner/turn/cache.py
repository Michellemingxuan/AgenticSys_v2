"""Layer-1 prior-turn recall: Q->A exact-match cache + KnowledgePoint lookup."""
from __future__ import annotations

import os
import time

# Cap on the per-session Q->A cache (see `_store_cached_qa` below). Moved
# here from `server.py` alongside the function that is its only consumer —
# it isn't a shared cross-module constant, so it doesn't belong in
# `runner/config.py`.
_QA_CACHE_MAX_ENTRIES = int(os.environ.get("QA_CACHE_MAX_ENTRIES", "1024"))


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

    Entries flagged ``no_replay`` are INVISIBLE here. The qa_cache does two
    jobs — it is the replay cache AND the sole source of episodic memory
    (``episodic.build_records`` reads it) — and the orchestrator-error fallback
    needs those jobs split: its partial, synthesis-failed answer must never be
    served again, but the next turn still has to know the exchange happened, or
    a subject-less follow-up ("think harder") binds to the wrong antecedent.
    Writing the entry with ``no_replay`` gets the memory without the replay. A
    later SUCCESSFUL run of the same question overwrites the key with an
    unflagged entry, which makes it replayable again — as it should be.
    """
    if not cache_key:
        return None
    cached = sess.qa_cache.get(cache_key)
    if cached is None:
        return None
    if cached.get("no_replay"):
        return None
    try:
        sess.qa_cache[cache_key] = sess.qa_cache.pop(cache_key)
    except Exception:
        pass
    return cached


def _store_cached_qa(sess, cache_key: str | None, value: dict,
                     *, asked_at: float | None = None) -> int:
    """Store a QA-cache entry and evict oldest entries beyond the cap.

    Returns the number of entries evicted. The cache is a speed optimization,
    not the audit source, so bounding it avoids long sessions retaining every
    answer payload forever.

    `asked_at` is the turn's WALL-CLOCK start, epoch seconds — what
    `/history` shows beside each question so a restored thread is not a list
    of undated rows. Callers pass their turn start; the default is now, which
    on this path means turn COMPLETION and so runs a turn-duration late. That
    is the acceptable fallback, not the intent — node_trace is the accurate
    source (see `NodeTraceStore.turn_started_at`) and this only has to cover
    turns it never saw.
    """
    if not cache_key:
        return 0
    sess._qa_turn_seq += 1
    value["turn_seq"] = sess._qa_turn_seq
    # Assigned, never `setdefault`: a cache-hit replay builds `value` as
    # `{**cached, ...}`, so an inherited `asked_at` would date the turn the
    # reviewer just asked to the ORIGINAL question's clock.
    value["asked_at"] = asked_at if asked_at is not None else time.time()
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


def _kp_backs_a_chart(kp: dict) -> bool:
    """True when this KP is one the Plots panel can actually render.

    Two shapes qualify — the same two ``finalize._collect_turn_charts``
    surfaces:
      * a rendered plot — ``image_path`` set by ``render_chart``;
      * a table KP — ``viz.kind == "table"`` (no image; the rows render as
        an HTML table).

    Shared by the collector and by ``_find_kp`` so the two cannot drift into
    disagreeing about which KP owns a (specialist, topic) key.
    """
    if not isinstance(kp, dict):
        return False
    if kp.get("image_path"):
        return True
    viz = kp.get("viz")
    return isinstance(viz, dict) and viz.get("kind") == "table"


def _find_kp(specialist_kps: dict, specialist: str, topic: str,
             turn_id: str) -> dict | None:
    """Return the KP for (specialist, topic) captured in this turn, or None.

    Used to enrich the chart SSE event with the KP's claim / source_call and
    to regenerate its Vega-Lite spec at emit time (see
    ``finalize._build_chart_payload``).

    PREFERENCE, not plain latest-wins. One topic can hold SEVERAL KPs in a
    turn: the specialist's own ``make_chart`` KP (carries ``viz``) and the
    auto-distiller's KP for the same finding (no ``viz``, no image). The
    distiller runs later, so a naive latest-wins scan returns the viz-less
    one — while ``_collect_turn_charts``, which filters to chart-backing KPs,
    keeps the chart. The payload then reached the frontend with ``kind: ""``
    and an empty ``url``, so a perfectly good table rendered as a BROKEN
    IMAGE (case 11854808010, turn 3dc0b6d549d8, "Returned Payments").

    So: latest CHART-BACKING KP wins; fall back to the latest overall when
    none backs a chart, which keeps every non-chart caller unchanged.
    """
    if not isinstance(specialist_kps, dict):
        return None
    kps = specialist_kps.get(specialist) or []
    found: dict | None = None
    found_charted: dict | None = None
    for kp in kps:
        if not isinstance(kp, dict):
            continue
        if kp.get("captured_at_turn") != turn_id:
            continue
        if kp.get("topic") != topic:
            continue
        found = kp  # latest-wins (chronological iteration)
        if _kp_backs_a_chart(kp):
            found_charted = kp
    return found_charted or found
