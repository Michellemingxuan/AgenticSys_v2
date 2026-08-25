"""Inspect and purge Amem memory — for when the STORE outlives the system.

Redeploying from a fresh zip gives you a new server, new logs and a new trace
DB, but Amem is external: same Qdrant, same collection, and by default the
same `org_id`/`user_id`. Memories are scoped by
`MemoryScope(org_id, user_id, case_id, ...)`, so a case id that existed in the
previous system is the SAME scope in the new one — and the new system reads
memories written by code and data that are gone.

    python tools/amem_purge.py --list                 # what is in there (safe)
    python tools/amem_purge.py --list --case 3661...  # one case
    python tools/amem_purge.py --purge --case 3661... # delete that case
    python tools/amem_purge.py --purge --all          # delete everything in scope

`--list` enumerates THE STORE, not just this deployment's `reports/` folder. A
leftover is by definition a case the current system has no folder for, so
asking `reports/` alone could never find one. Anything the store holds without
a matching folder is flagged ORPHAN, and `--purge --all` covers it.

Dry-run by default: nothing is deleted without `--purge`, and `--purge --all`
asks for confirmation unless `--yes` is given.

PREFER ISOLATION OVER DELETION. If each system instance gets its own namespace
there is nothing to clean up and no chance of deleting a colleague's data:

    AMEM_COLLECTION_NAME=amem_memories_<instance>   # separate Qdrant collection
    AMEM_ORG_ID=<instance>                          # or a separate scope

Deletion is the right tool for reclaiming space or scrubbing one case, not for
routine redeploys.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from memory.config import AmemConfig          # noqa: E402
from memory.factory import build_amem_manager  # noqa: E402
from memory.rewind import delete_case_memory   # noqa: E402
from memory.scope import build_scope           # noqa: E402


def _known_cases() -> list[str]:
    """Case ids this deployment knows about, from the reports folder."""
    reports = Path(__file__).resolve().parent.parent / "reports"
    if not reports.is_dir():
        return []
    return sorted(p.name for p in reports.iterdir()
                  if p.is_dir() and not p.name.startswith("_"))


#: Records per `list_memories` page. The store is enumerated in pages because
#: a single unbounded call would have to materialise every memory at once.
_PAGE = 512


def _store_cases(amem, cfg) -> dict[str, int] | None:
    """Case ids the STORE holds for this org/user, counted.

    Returns None when the store could not be enumerated — distinct from an
    empty dict, which means the store answered and holds nothing. The caller
    must not report "no leftovers" on the strength of a failed lookup.

    Scoped to `cfg.org_id`/`cfg.user_id` deliberately. A shared Qdrant may hold
    a colleague's memories under a different scope, and those are neither ours
    to list nor ours to delete.
    """
    counts: dict[str, int] = {}
    offset = 0
    try:
        while True:
            batch = list(amem.list_memories(
                limit=_PAGE, offset=offset, include_working=True) or [])
            for rec in batch:
                scope = getattr(rec, "scope", None)
                if scope is None:
                    continue
                if (scope.org_id, scope.user_id) != (cfg.org_id, cfg.user_id):
                    continue
                if scope.case_id:
                    counts[scope.case_id] = counts.get(scope.case_id, 0) + 1
            # A short page is the last page; `offset` past the end returns [].
            if len(batch) < _PAGE:
                return counts
            offset += len(batch)
    except Exception as exc:  # noqa: BLE001
        print(f"  store enumeration failed — {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    ap.add_argument("--list", action="store_true", help="show records, delete nothing")
    ap.add_argument("--purge", action="store_true", help="actually delete")
    ap.add_argument("--case", help="one case id")
    ap.add_argument("--all", action="store_true",
                    help="every case in the store or in reports/")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    if not (args.list or args.purge):
        ap.error("nothing to do — pass --list or --purge")
    if args.purge and not (args.case or args.all):
        ap.error("--purge needs --case <id> or --all")

    cfg = AmemConfig.from_env()
    print(f"store       {cfg.store_url}")
    print(f"collection  {cfg.collection_name}")
    print(f"scope       org={cfg.org_id} user={cfg.user_id}")
    print(f"enabled     {cfg.enabled}\n")

    amem = build_amem_manager(cfg, backend="openai")
    if type(amem).__name__ == "NullAmemManager":
        print("Amem is unavailable (disabled, or the store is unreachable) — "
              "nothing to inspect or purge.")
        return 1

    known = set(_known_cases())
    orphans: set[str] = set()
    if args.case:
        cases = [args.case]
    else:
        stored = _store_cases(amem, cfg)
        if stored is None:
            # Say so rather than degrade quietly: a reports/-only listing
            # cannot show leftovers, and silence here would read as "clean".
            print("  ! could not enumerate the store — showing only cases this\n"
                  "    deployment has a reports/ folder for. Leftovers from a\n"
                  "    previous system would NOT appear below.\n")
            cases = sorted(known)
        else:
            orphans = set(stored) - known
            cases = sorted(known | set(stored))
    if not cases:
        print("No case ids found. Pass --case explicitly.")
        return 1

    # Always show what is there BEFORE touching anything, so a purge is a
    # decision rather than a leap.
    total = 0
    counts: dict[str, int] = {}
    for case_id in cases:
        try:
            recs = list(amem.list_memories(
                scope=build_scope(cfg, case_id), include_working=True) or [])
        except Exception as exc:  # noqa: BLE001
            print(f"  {case_id:<20} list failed — {type(exc).__name__}: {exc}")
            continue
        counts[case_id] = len(recs)
        total += len(recs)
        tag = "   ORPHAN — no reports/ folder" if case_id in orphans else ""
        print(f"  {case_id:<20} {len(recs):>5} record(s){tag}")
    print(f"\n  {'TOTAL':<20} {total:>5} record(s)")
    if orphans:
        print(f"  {len(orphans)} orphan case(s) — memory this deployment has "
              f"no data for.")
    print()

    if not args.purge:
        print("Dry run — nothing deleted. Add --purge to delete.")
        return 0
    if total == 0:
        print("Nothing to delete.")
        return 0

    if args.all and not args.yes:
        note = f", {len(orphans)} of them orphaned" if orphans else ""
        print(f"About to delete {total} record(s) across "
              f"{len(counts)} case(s){note}.")
        if input("Type 'delete' to confirm: ").strip() != "delete":
            print("Aborted.")
            return 1

    deleted = failed = 0
    for case_id in counts:
        out = delete_case_memory(amem, cfg, case_id=case_id)
        deleted += out.deleted
        failed += out.failed
        print(f"  {case_id:<20} deleted {out.deleted}, failed {out.failed}")
    print(f"\ndeleted {deleted}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
