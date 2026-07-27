#!/usr/bin/env python
"""Open the Amem Qdrant store directly (no viewer, no app code) and dump a
case's memory records readably.

Usage:
  python -m tools.amem_inspect                 # all records, grouped by case
  python -m tools.amem_inspect 366132845011    # one case
  python -m tools.amem_inspect 366132845011 --raw   # full payload JSON per point

Talks straight to Qdrant on AMEM_STORE_URL (default http://127.0.0.1:6333),
collection 'amem_memories' (override with AMEM_COLLECTION). Nothing here
depends on the AgenticSys or Amem packages — it is a pure store reader, handy
for confirming what actually got persisted independent of the web viewer.
"""
import argparse
import json
import os

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

COLL = os.environ.get("AMEM_COLLECTION", "amem_memories")
URL = os.environ.get("AMEM_STORE_URL", "http://127.0.0.1:6333")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case_id", nargs="?", help="filter to one case_id")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also show record content, KP claim numbers, and tool params")
    ap.add_argument("--raw", action="store_true", help="print full payload JSON")
    args = ap.parse_args()

    client = QdrantClient(url=URL)
    flt = None
    if args.case_id:
        flt = qm.Filter(must=[qm.FieldCondition(
            key="case_id", match=qm.MatchValue(value=args.case_id))])

    points, _ = client.scroll(
        COLL, scroll_filter=flt, limit=1000,
        with_payload=True, with_vectors=False,
    )
    print(f"{len(points)} points in {COLL} @ {URL}"
          + (f" for case {args.case_id}" if args.case_id else "") + "\n")

    # oldest-first so turns read chronologically
    points.sort(key=lambda p: (p.payload.get("case_id") or "",
                               p.payload.get("created_at") or ""))
    for p in points:
        pl = p.payload
        if args.raw:
            print(json.dumps(pl, indent=2, default=str))
            print("-" * 70)
            continue
        md = pl.get("metadata") or {}
        kps = md.get("knowledge_points") or []
        tcs = md.get("tool_calls") or []
        # Header: what item is stored, for whom, when.
        print(f"[{pl.get('level')}/{pl.get('kind')}] case={pl.get('case_id')} "
              f"agent={pl.get('agent_id')} turn={pl.get('turn_id')} "
              f"created={pl.get('created_at')}")
        # Question + Answer come from metadata (raw_question / raw_answer). Show
        # BOTH for every Q&A turn so the question is always visible, not just the
        # answer. Short by default, full with -v.
        def _clip(s, n):
            s = str(s or "").replace("\n", " ")
            return s if args.verbose else (s[:n] + ("…" if len(s) > n else ""))
        # Amem nests the Q&A under metadata["conversation"].
        conv = md.get("conversation") or {}
        rq, ra = conv.get("raw_question"), conv.get("raw_answer")
        if rq:
            print(f"    Q: {_clip(rq, 120)}")
        if ra:
            print(f"    A: {_clip(ra, 130)}")
        # Records with no Q&A pair (e.g. case_summary): fall back to content.
        if not rq and not ra:
            content = (pl.get("content") or "").replace("\n", " ")
            if content:
                print(f"    {content if args.verbose else content[:130]}")
        # KPs: topic + short claim by default; the (long) numbers array only with -v.
        for kp in kps:
            line = f"    KP[{kp.get('topic')}]"
            claim = str(kp.get("claim") or "").replace("\n", " ")
            if claim:
                line += f": {claim[:100]}"
            if args.verbose and kp.get("numbers"):
                line += f"  numbers={kp['numbers']}"
            print(line)
        # Tool calls: just the function name by default; params only with -v.
        for tc in tcs:
            fn = tc.get("func")
            if args.verbose:
                print(f"    tool_call: {fn}({json.dumps(tc.get('params', {}))[:100]})")
            else:
                print(f"    tool_call: {fn}")
        for td in (md.get("team_dispatch") or []):
            print(f"    dispatch: {td.get('specialist')} <- "
                  f"subq={str(td.get('sub_question'))[:70]!r} "
                  f"concepts={td.get('concepts')}")
        print()


if __name__ == "__main__":
    main()
