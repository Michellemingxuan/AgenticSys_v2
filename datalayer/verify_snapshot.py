"""datalayer/verify_snapshot.py
Private versioning of verified context + profiles via local snapshots.

CLI:  python -m datalayer.verify_snapshot <cmd> [options]

Subcommands
-----------
snapshot [--label X]
    Create .catalog_verified/<UTC-timestamp>[-label]/ with copies of
    context/*.txt, config/data_profiles/*.yaml, .provenance.json (if present),
    and a manifest.json with sha256 hashes + git commit.

list
    Print all snapshots (newest-first) with created, label, file count.

diff [<snapshot>|latest]
    Compare current state against a snapshot: ADDED / REMOVED / CHANGED files
    (by sha256); CHANGED text files include a unified line diff.

restore <snapshot> [--force]
    DEFAULT IS DRY-RUN — prints what WOULD be written and exits without writing.
    Only --force actually writes files back.

Core functions accept explicit path arguments so tests can use tmp dirs without
touching real context/ or config/data_profiles/.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Defaults (real paths used by the CLI)
# ---------------------------------------------------------------------------

_DEFAULT_CONTEXT_DIR = "context"
_DEFAULT_PROFILE_DIR = "config/data_profiles"
_DEFAULT_PROVENANCE = "config/data_profiles/.provenance.json"
_DEFAULT_SNAPSHOT_ROOT = ".catalog_verified"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _git_short_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _collect_current_files(
    context_dir: str,
    profile_dir: str,
    provenance_path: str,
) -> dict[str, Path]:
    """Return {relative_path_str: absolute Path} for all files to snapshot."""
    files: dict[str, Path] = {}

    ctx = Path(context_dir)
    if ctx.is_dir():
        for p in sorted(ctx.glob("*.txt")):
            files[f"context/{p.name}"] = p

    prof = Path(profile_dir)
    if prof.is_dir():
        for p in sorted(prof.glob("*.yaml")):
            files[f"config/data_profiles/{p.name}"] = p

    prov = Path(provenance_path)
    if prov.exists():
        files["config/data_profiles/.provenance.json"] = prov

    return files


def _resolve_snapshot(target: str, snapshot_root: str) -> str:
    """Resolve 'latest' to the actual newest snapshot name, else return as-is.

    Delegates to cmd_list so that 'latest' is always the entry at index 0
    of the newest-first list, matching what `list` subcommand shows.
    """
    if target != "latest":
        return target
    # cmd_list returns newest-first sorted by created (then dir name).
    # We call it directly to avoid duplicating sort logic.
    listing = cmd_list(snapshot_root=snapshot_root)
    if not listing:
        raise ValueError("No snapshots found in snapshot_root.")
    return listing[0]["name"]


# ---------------------------------------------------------------------------
# Core functions (accept explicit paths for testability)
# ---------------------------------------------------------------------------

def cmd_snapshot(
    label: str | None = None,
    context_dir: str = _DEFAULT_CONTEXT_DIR,
    profile_dir: str = _DEFAULT_PROFILE_DIR,
    provenance_path: str = _DEFAULT_PROVENANCE,
    snapshot_root: str = _DEFAULT_SNAPSHOT_ROOT,
) -> dict[str, Any]:
    """Create a snapshot. Returns dict with snapshot_dir and manifest keys."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    dir_name = label if label else ts
    snap_dir = Path(snapshot_root) / dir_name
    # If the dir already exists (same label called twice), clear stale content
    # so a re-snapshot is always clean — no FileExistsError.
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)

    current_files = _collect_current_files(context_dir, profile_dir, provenance_path)

    manifest_files: dict[str, str] = {}
    for relpath, src in current_files.items():
        dest = snap_dir / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        manifest_files[relpath] = _sha256(dest)

    manifest: dict[str, Any] = {
        "created": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "files": manifest_files,
        "git_commit": _git_short_commit(),
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return {"snapshot_dir": str(snap_dir), "manifest": manifest}


def cmd_list(
    snapshot_root: str = _DEFAULT_SNAPSHOT_ROOT,
    **_ignored: Any,
) -> list[dict[str, Any]]:
    """Return snapshots sorted newest-first."""
    root = Path(snapshot_root)
    if not root.is_dir():
        return []

    entries = []
    for d in root.iterdir():
        mf = d / "manifest.json"
        if d.is_dir() and mf.exists():
            try:
                manifest = json.loads(mf.read_text())
                entries.append({
                    "name": d.name,
                    "created": manifest.get("created", manifest.get("created_utc", "")),
                    "label": manifest.get("label"),
                    "file_count": len(manifest.get("files", {})),
                })
            except Exception:
                pass

    # Newest-first: sort by (created, name) descending.
    # created is an ISO datetime string from manifest.json (microsecond precision),
    # so it correctly orders two snapshots that share the same UTC second in their name.
    entries.sort(key=lambda e: (e["created"], e["name"]), reverse=True)
    return entries


def cmd_diff(
    target: str = "latest",
    context_dir: str = _DEFAULT_CONTEXT_DIR,
    profile_dir: str = _DEFAULT_PROFILE_DIR,
    provenance_path: str = _DEFAULT_PROVENANCE,
    snapshot_root: str = _DEFAULT_SNAPSHOT_ROOT,
) -> dict[str, Any]:
    """Compare current state against a snapshot.

    Returns:
        {
          "snapshot_used": <name>,
          "added":   [relpath, ...],         # in current but not in snapshot
          "removed": [relpath, ...],         # in snapshot but not in current
          "changed": [{"relpath": relpath, "diff": unified_diff_str}, ...],
        }
    """
    snap_name = _resolve_snapshot(target, snapshot_root)
    snap_dir = Path(snapshot_root) / snap_name
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    snap_files: dict[str, str] = manifest["files"]  # relpath → sha256

    current_files = _collect_current_files(context_dir, profile_dir, provenance_path)
    current_shas = {rel: _sha256(p) for rel, p in current_files.items()}

    snap_set = set(snap_files)
    cur_set = set(current_shas)

    added = sorted(cur_set - snap_set)
    removed = sorted(snap_set - cur_set)
    changed: list[dict[str, Any]] = []

    for rel in sorted(snap_set & cur_set):
        if snap_files[rel] != current_shas[rel]:
            # Produce unified diff for text files
            snap_text_path = snap_dir / rel
            cur_path = current_files[rel]
            try:
                snap_lines = snap_text_path.read_text(errors="replace").splitlines(keepends=True)
                cur_lines = cur_path.read_text(errors="replace").splitlines(keepends=True)
                diff = "".join(
                    difflib.unified_diff(snap_lines, cur_lines, fromfile=f"snap/{rel}", tofile=f"current/{rel}")
                )
            except Exception:
                diff = "<binary or unreadable>"
            changed.append({"relpath": rel, "diff": diff})

    return {
        "snapshot_used": snap_name,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def cmd_restore(
    snapshot: str,
    force: bool = False,
    context_dir: str = _DEFAULT_CONTEXT_DIR,
    profile_dir: str = _DEFAULT_PROFILE_DIR,
    provenance_path: str = _DEFAULT_PROVENANCE,
    snapshot_root: str = _DEFAULT_SNAPSHOT_ROOT,
) -> dict[str, Any]:
    """Restore snapshot files to current dirs.

    DEFAULT IS DRY-RUN (force=False): prints the plan and returns it without
    writing any files. Only force=True actually writes.

    Returns:
        {
          "dry_run": bool,
          "snapshot": <name>,
          "plan": [{"src": str, "dest": str}, ...],
        }
    """
    snap_dir = Path(snapshot_root) / snapshot
    if not snap_dir.is_dir():
        raise FileNotFoundError(f"Snapshot not found: {snap_dir}")

    manifest = json.loads((snap_dir / "manifest.json").read_text())
    snap_files: dict[str, str] = manifest["files"]

    # Determine destination roots from explicit dir arguments
    # We map each relpath prefix to the appropriate base dir:
    #   context/...                       → context_dir
    #   config/data_profiles/.prov...     → provenance_path (parent)
    #   config/data_profiles/...          → profile_dir
    def _dest_for(relpath: str) -> Path:
        if relpath.startswith("context/"):
            return Path(context_dir) / relpath[len("context/"):]
        if relpath == "config/data_profiles/.provenance.json":
            return Path(provenance_path)
        if relpath.startswith("config/data_profiles/"):
            return Path(profile_dir) / relpath[len("config/data_profiles/"):]
        # Fallback: reconstruct path relative to snapshot_root's parent
        return Path(snapshot_root).parent / relpath

    plan = []
    would_overwrite: list[str] = []
    would_create: list[str] = []
    for relpath in sorted(snap_files):
        src = snap_dir / relpath
        dest = _dest_for(relpath)
        plan.append({"src": str(src), "dest": str(dest)})
        if dest.exists():
            would_overwrite.append(str(dest))
        else:
            would_create.append(str(dest))

    if force:
        for item in plan:
            dest = Path(item["dest"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item["src"], dest)

    return {
        "dry_run": not force,
        "snapshot": snapshot,
        "plan": plan,
        "would_overwrite": would_overwrite,
        "would_create": would_create,
    }


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _print_snapshot(result: dict) -> None:
    print(f"Created: {result['snapshot_dir']}")
    print(f"Files:   {len(result['manifest']['files'])}")
    print(f"Git:     {result['manifest']['git_commit'] or '(none)'}")


def _print_list(entries: list[dict]) -> None:
    if not entries:
        print("(no snapshots)")
        return
    for e in entries:
        label_str = f"  label={e['label']}" if e["label"] else ""
        print(f"  {e['name']}  {e['created']}{label_str}  [{e['file_count']} files]")


def _print_diff(result: dict) -> None:
    print(f"Snapshot: {result['snapshot_used']}")
    if result["added"]:
        print("ADDED:")
        for f in result["added"]:
            print(f"  + {f}")
    if result["removed"]:
        print("REMOVED:")
        for f in result["removed"]:
            print(f"  - {f}")
    if result["changed"]:
        print("CHANGED:")
        for c in result["changed"]:
            print(f"  ~ {c['relpath']}")
            if c["diff"]:
                for line in c["diff"].splitlines():
                    print(f"    {line}")
    if not result["added"] and not result["removed"] and not result["changed"]:
        print("No differences.")


def _print_restore(result: dict) -> None:
    if result["dry_run"]:
        print(f"Restore DRY-RUN — snapshot: {result['snapshot']} (dry run — pass --force to apply)")
        would_overwrite_set = set(result.get("would_overwrite", []))
        for item in result["plan"]:
            action = "WOULD overwrite" if item["dest"] in would_overwrite_set else "WOULD create"
            print(f"  {action}: {item['dest']}")
    else:
        print(f"Restore FORCE (files written) — snapshot: {result['snapshot']}")
        for item in result["plan"]:
            print(f"  Wrote: {item['dest']}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m datalayer.verify_snapshot",
        description="Private versioning of verified context + profiles.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Create a new snapshot.")
    p_snap.add_argument("--label", default=None, help="Optional label appended to snapshot name.")

    # list
    sub.add_parser("list", help="List existing snapshots (newest-first).")

    # diff
    p_diff = sub.add_parser("diff", help="Diff current state against a snapshot.")
    p_diff.add_argument("target", nargs="?", default="latest",
                        help="Snapshot name or 'latest' (default).")

    # restore
    p_restore = sub.add_parser("restore", help="Restore a snapshot. Default: dry-run only.")
    p_restore.add_argument("snapshot", help="Snapshot name to restore.")
    p_restore.add_argument("--force", action="store_true",
                           help="Actually write files (default is dry-run, no write).")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    kwargs = {
        "context_dir": _DEFAULT_CONTEXT_DIR,
        "profile_dir": _DEFAULT_PROFILE_DIR,
        "provenance_path": _DEFAULT_PROVENANCE,
        "snapshot_root": _DEFAULT_SNAPSHOT_ROOT,
    }

    if args.command == "snapshot":
        result = cmd_snapshot(label=args.label, **kwargs)
        _print_snapshot(result)

    elif args.command == "list":
        entries = cmd_list(**kwargs)
        _print_list(entries)

    elif args.command == "diff":
        result = cmd_diff(target=args.target, **kwargs)
        _print_diff(result)

    elif args.command == "restore":
        result = cmd_restore(snapshot=args.snapshot, force=args.force, **kwargs)
        _print_restore(result)


if __name__ == "__main__":
    main()
