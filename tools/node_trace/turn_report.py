"""CLI tree reader for the node_trace SQLite store.

Usage:
    python -m tools.node_trace.turn_report --chat <chat_id> --turn <turn_id>
    python -m tools.node_trace.turn_report --last
    python -m tools.node_trace.turn_report --last --json
    python -m tools.node_trace.turn_report --last --full-excerpts
    python -m tools.node_trace.turn_report --last --csv                # writes logs/turn_<turn_id>.csv
    python -m tools.node_trace.turn_report --last --csv path/out.csv   # explicit path
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from tools.node_trace._io import open_db


_CSV_COLUMNS = [
    "id", "chat_id", "case_id", "turn_id", "node", "parent_id", "depth",
    "started_at", "ended_at", "duration_ms",
    "queue_wait_ms", "llm_call_ms", "ttft_ms", "overhead_ms",
    "model",
    "prompt_chars", "prompt_tokens", "cached_input_tokens", "system_prompt_chars",
    "completion_chars", "completion_tokens", "reasoning_tokens", "total_tokens",
    "cost_usd",
    "prompt_excerpt", "completion_excerpt",
    "outcome", "error_type", "tags", "extra_json",
]


_DEFAULT_DB = Path("logs/node_traces.db")


def _fetch_rows(db: Path, chat_id: str | None, turn_id: str | None) -> list[dict]:
    import sqlite3 as _sqlite
    conn = open_db(db)
    # If the table doesn't exist yet (fresh / missing DB), behave as empty.
    try:
        if chat_id is None and turn_id is None:
            last = conn.execute(
                "SELECT chat_id, turn_id FROM node_trace "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if last is None:
                return []
            chat_id, turn_id = last["chat_id"], last["turn_id"]
    except _sqlite.OperationalError:
        return []
    where = []
    params: list[Any] = []
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    if turn_id is not None:
        where.append("turn_id = ?")
        params.append(turn_id)
    sql = "SELECT * FROM node_trace"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at"
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except _sqlite.OperationalError:
        return []


def build_tree(
    db: Path, *, chat_id: str | None = None, turn_id: str | None = None,
) -> list[dict]:
    rows = _fetch_rows(db, chat_id, turn_id)
    by_id = {r["id"]: {**r, "children": []} for r in rows}
    roots: list[dict] = []
    for r in by_id.values():
        pid = r.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(r)
        else:
            roots.append(r)
    return roots


def _flatten(tree: list[dict]) -> list[dict]:
    out: list[dict] = []
    for n in tree:
        out.append(n)
        out.extend(_flatten(n.get("children") or []))
    return out


def write_csv(rows: list[dict], path: Path) -> Path:
    """Write the (already-flattened) rows to a CSV, ordered by started_at.

    Returns the resolved output path so the caller can echo it.
    Strips the ``children`` field that the tree builder adds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: r.get("started_at") or "")
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in ordered:
            writer.writerow({k: r.get(k) for k in _CSV_COLUMNS})
    return path


def render_text(tree: list[dict], full_excerpts: bool = False, indent: int = 0) -> str:
    lines: list[str] = []
    for node in tree:
        prefix = "  " * indent + ("├─ " if indent > 0 else "")
        excerpt = node.get("prompt_excerpt") or ""
        if not full_excerpts and len(excerpt) > 80:
            excerpt = excerpt[:80].replace("\n", " ") + "…"
        excerpt = excerpt.replace("\n", " ")
        lines.append(
            f"{prefix}{node['node']:<46} "
            f"{(node.get('duration_ms') or 0)/1000:>6.1f}s  "
            f"in={node.get('prompt_tokens') or '-':<6} "
            f"out={node.get('completion_tokens') or '-':<5}  "
            f"{excerpt}"
        )
        if node["children"]:
            lines.append(render_text(node["children"], full_excerpts, indent + 1))
    return "\n".join(l for l in lines if l)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(_DEFAULT_DB))
    p.add_argument("--chat")
    p.add_argument("--turn")
    p.add_argument("--last", action="store_true",
                   help="latest turn across the DB (overrides --chat/--turn)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--full-excerpts", action="store_true")
    p.add_argument(
        "--csv", nargs="?", const="__default__", default=None,
        help="export rows to CSV. Without an argument writes "
             "logs/turn_<turn_id>.csv; with a path writes there.",
    )
    args = p.parse_args()

    chat = None if args.last else args.chat
    turn = None if args.last else args.turn
    tree = build_tree(Path(args.db), chat_id=chat, turn_id=turn)
    if not tree:
        print(f"No rows in {args.db} for chat={chat} turn={turn}.")
        return
    head = tree[0]

    if args.csv is not None:
        if args.csv == "__default__":
            out_path = Path("logs") / f"turn_{head['turn_id']}.csv"
        else:
            out_path = Path(args.csv)
        written = write_csv(_flatten(tree), out_path)
        print(f"wrote {len(_flatten(tree))} rows → {written}")
        return

    if args.json:
        print(json.dumps(tree, indent=2, default=str))
        return
    total_in = sum((r.get("prompt_tokens") or 0) for r in _flatten(tree))
    total_out = sum((r.get("completion_tokens") or 0) for r in _flatten(tree))
    total_s = sum((r.get("duration_ms") or 0) for r in tree) / 1000
    print(
        f"chat {head['chat_id']}  turn {head['turn_id']}  "
        f"total={total_s:.1f}s  in={total_in} tok  out={total_out} tok"
    )
    print(render_text(tree, args.full_excerpts))


if __name__ == "__main__":
    main()
