"""One-time repair for case ids written before `normalize_case_id` existed.

WHY THIS EXISTS. The case id used to be whatever the data folder was named,
so a directory called ``"11854808010 "`` (trailing space) put a PADDED id on
every durable record it touched. `datalayer.gateway.normalize_case_id` now
canonicalizes at ingress, which fixes everything written from here on — but
it also ORPHANS what was already written: the running system asks for
``"11854808010"`` while the old rows say ``"11854808010 "``, and nothing
matches them any more.

The user-visible symptom is a clear-history that appears to do nothing:

    03:30  case_id "11854808010 "  trace_rows_cleared: 54   (pre-fix)
    05:28  case_id "11854808010"   trace_rows_cleared: 33   (post-fix)
           ... 48 rows under the padded id survive both

Clear-history is working; it simply cannot see the pre-fix rows. Same for
Amem, whose points carry the padded `case_id` in their payload, and for
`reports/`, where one case has two directories.

TWO MODES, both dry-run by default:

  `--mode repoint` (default)  Rewrite the stale id to its canonical form. No
      history is destroyed — the records simply become reachable again, and
      the case's normal controls (Clear this case, rewind) can then act on
      them. Use this when the orphaned turns are still wanted.

  `--mode delete`  Drop the orphaned records outright. Use this when the
      orphans are residue of a case the reviewer already cleared: repointing
      them would only resurrect conversations into a thread that is meant to
      be empty, forcing a second Clear to remove what was never wanted back.

Usage — nothing is written without `--apply`:

    python -m tools.migrate_case_ids                        # report (repoint)
    python -m tools.migrate_case_ids --mode delete          # report (delete)
    python -m tools.migrate_case_ids --mode delete --apply  # do it
    python -m tools.migrate_case_ids --apply --only node_trace

Idempotent in both modes: a second run finds nothing to do.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from datalayer.gateway import normalize_case_id
from runner.config import _NODE_TRACE_DB_PATH, _REPORTS_DIR

# `conversation_id` / `chat_id` are DERIVED from the case id
# (`runner.identity.compose_conversation_id` = f"{case}::{user}::{pillar}"),
# so a padded case id is baked into their prefix too. Migrating case_id alone
# would leave the trace viewer showing the old rows under a different
# conversation than the new ones — technically reachable, visibly split.
_SEP = "::"


def _repoint_prefix(value: str | None, stale: str, clean: str) -> str | None:
    """Rewrite a derived id whose first `::`-segment is the stale case id."""
    if not value:
        return value
    if value == stale:
        return clean
    if value.startswith(stale + _SEP):
        return clean + value[len(stale):]
    return value


# ── node-trace (SQLite) ─────────────────────────────────────────────────────


def migrate_node_trace(db_path: str, apply: bool,
                       mode: str = "repoint") -> list[str]:
    notes: list[str] = []
    if not Path(db_path).exists():
        return [f"node_trace: no db at {db_path} — nothing to do"]
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("node_trace", "session_snapshot"):
            if table not in tables:
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            rows = conn.execute(
                f"SELECT case_id, COUNT(*) FROM {table} GROUP BY case_id"
            ).fetchall()
            for stale, n in rows:
                clean = normalize_case_id(stale)
                if clean == stale or not clean:
                    continue
                if mode == "delete":
                    notes.append(f"node_trace/{table}: DELETE {stale!r} ({n} rows)")
                    if apply:
                        conn.execute(
                            f"DELETE FROM {table} WHERE case_id = ?", (stale,))
                    continue
                notes.append(f"node_trace/{table}: {stale!r} -> {clean!r} ({n} rows)")
                if not apply:
                    continue
                conn.execute(
                    f"UPDATE {table} SET case_id = ? WHERE case_id = ?",
                    (clean, stale))
                # Repoint the derived ids so migrated rows group with new ones.
                for col in ("chat_id", "conversation_id"):
                    if col not in cols:
                        continue
                    for (val,) in conn.execute(
                        f"SELECT DISTINCT {col} FROM {table} WHERE case_id = ?",
                        (clean,),
                    ).fetchall():
                        new = _repoint_prefix(val, stale, clean)
                        if new != val:
                            conn.execute(
                                f"UPDATE {table} SET {col} = ? "
                                f"WHERE {col} = ? AND case_id = ?",
                                (new, val, clean))
        if apply:
            conn.commit()
    finally:
        conn.close()
    return notes or ["node_trace: all case ids already canonical"]


# ── Amem (Qdrant payloads) ──────────────────────────────────────────────────


def _qdrant(url: str, path: str, body: dict | None) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def migrate_amem(store_url: str, collection: str, apply: bool,
                 mode: str = "repoint") -> list[str]:
    """Repoint (or delete) points whose top-level `case_id` payload is stale.

    `metadata.session_id` also embeds the padded id, but it is left ALONE on
    purpose: it names a real historical log file (`logs/case-<sid>.jsonl`) and
    rewriting it would break that correspondence. Only `case_id` routes
    reads/deletes, and only `case_id` is wrong.
    """
    notes: list[str] = []
    stale_ids: dict[str, list] = {}
    offset = None
    try:
        while True:
            body: dict = {"limit": 256, "with_payload": True, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            res = _qdrant(store_url,
                          f"/collections/{collection}/points/scroll", body)["result"]
            for p in res.get("points") or []:
                cid = (p.get("payload") or {}).get("case_id")
                if not isinstance(cid, str):
                    continue
                if normalize_case_id(cid) != cid:
                    stale_ids.setdefault(cid, []).append(p["id"])
            offset = res.get("next_page_offset")
            if offset is None:
                break
    except (urllib.error.URLError, OSError, KeyError) as exc:
        return [f"amem: store unreachable at {store_url} ({type(exc).__name__}) "
                f"— skipped, NOT migrated"]

    if not stale_ids:
        return ["amem: all case ids already canonical"]

    for stale, ids in stale_ids.items():
        clean = normalize_case_id(stale)
        if mode == "delete":
            notes.append(f"amem: DELETE {stale!r} ({len(ids)} points)")
            if apply:
                _qdrant(store_url,
                        f"/collections/{collection}/points/delete?wait=true",
                        {"points": ids})
            continue
        notes.append(f"amem: {stale!r} -> {clean!r} ({len(ids)} points)")
        if not apply:
            continue
        _qdrant(store_url,
                f"/collections/{collection}/points/payload?wait=true",
                {"payload": {"case_id": clean}, "points": ids})
    return notes


# ── reports/ directories ────────────────────────────────────────────────────


def migrate_reports(reports_dir: Path, apply: bool,
                    mode: str = "repoint") -> list[str]:
    """Merge `reports/<padded>/` into `reports/<clean>/`, or delete it.

    Merge never overwrites: a file already present under the clean id wins and
    the padded copy is reported, not clobbered. Charts and curated reports are
    the reviewer's artifacts, not ours to silently replace.

    In `delete` mode the padded directory is removed outright — its contents
    are generated chart images belonging to turns being dropped in the same
    run, so keeping them would strand files no turn references.
    """
    notes: list[str] = []
    if not reports_dir.is_dir():
        return [f"reports: no {reports_dir} — nothing to do"]
    for src in sorted(reports_dir.iterdir()):
        if not src.is_dir():
            continue
        clean = normalize_case_id(src.name)
        if clean == src.name or not clean:
            continue
        if mode == "delete":
            n_files = sum(1 for p in src.rglob("*") if p.is_file())
            notes.append(f"reports: DELETE {src.name!r} ({n_files} files)")
            if apply:
                shutil.rmtree(src, ignore_errors=True)
            continue
        dst = reports_dir / clean
        moved = skipped = 0
        for item in sorted(src.rglob("*")):
            if not item.is_file():
                continue
            target = dst / item.relative_to(src)
            if target.exists():
                skipped += 1
                continue
            moved += 1
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(target))
        notes.append(
            f"reports: {src.name!r} -> {clean!r} ({moved} files moved"
            + (f", {skipped} already present and kept" if skipped else "") + ")")
        if apply and moved and not any(p.is_file() for p in src.rglob("*")):
            shutil.rmtree(src, ignore_errors=True)
            notes.append(f"reports: removed now-empty {src.name!r}")
    return notes or ["reports: all case ids already canonical"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--mode", choices=("repoint", "delete"), default="repoint",
                    help="repoint stale ids to canonical (default), or delete "
                         "the orphaned records outright")
    ap.add_argument("--only", choices=("node_trace", "amem", "reports"),
                    help="migrate a single target")
    ap.add_argument("--db", default=_NODE_TRACE_DB_PATH)
    ap.add_argument("--amem-url", default=None,
                    help="default: AMEM_STORE_URL or http://127.0.0.1:6333")
    args = ap.parse_args()

    import os
    amem_url = args.amem_url or os.environ.get(
        "AMEM_STORE_URL", "http://127.0.0.1:6333")
    collection = os.environ.get("AMEM_COLLECTION_NAME", "amem_memories")

    print(f"DRY RUN (mode={args.mode}) — nothing written. "
          f"Re-run with --apply to commit.\n"
          if not args.apply else f"APPLYING changes (mode={args.mode}).\n")
    notes: list[str] = []
    if args.only in (None, "node_trace"):
        notes += migrate_node_trace(args.db, args.apply, args.mode)
    if args.only in (None, "amem"):
        notes += migrate_amem(amem_url, collection, args.apply, args.mode)
    if args.only in (None, "reports"):
        notes += migrate_reports(_REPORTS_DIR, args.apply, args.mode)
    for n in notes:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
