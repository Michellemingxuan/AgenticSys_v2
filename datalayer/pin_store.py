"""Persistence for pinned insights / figures, and the opportunities they become.

Why a separate store rather than files in ``reports/<case>/``: that folder is
the report agent's evidence base. Anything written there is read back by the
agent on the next turn, so a reviewer pinning a figure would silently change
what the agent believes the curated report says. Pins are reviewer annotation
ABOUT the case, not evidence FROM it, and they stay out of that folder.

SQLite rather than JSON because the Flask server is threaded (``threaded=True``)
and several tabs on the same case can pin concurrently; a read-modify-write
over a JSON file loses one of them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

def _resolve(raw: str) -> Path:
    """Expand `~` and `$VAR` before treating a string as a path.

    python-dotenv hands back the LITERAL value, so a `.env` line like
    `NODE_TRACE_DB=$HOME/agenticsys-traces/node_traces.db` arrives with the
    `$HOME` unexpanded — and SQLite then creates a directory actually named
    `$HOME` next to the working directory. `runner/config.py` carries the
    same guard for the same reason; this is the second consumer of that env
    var and needs it too.
    """
    return Path(os.path.expanduser(os.path.expandvars(raw)))


# Sits beside the node-trace db so both live under one user-writable dir.
# Same env-var-with-default convention as `tools/node_trace/viewer.py`.
_DEFAULT_DB = _resolve(
    os.environ.get("NODE_TRACE_DB", "logs/node_traces.db")
).parent / "pins.db"

DB_PATH = _resolve(os.environ.get("PIN_DB", str(_DEFAULT_DB)))

# What a pin may be. `insight` is a claim lifted from an answer or a report
# bullet; `figure` is one chart on one turn.
PIN_KINDS = ("insight", "figure")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
    pin_id      TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    -- Provenance. A pin the reviewer cannot trace back to its turn (or to
    -- the report section it came from) is an unsourced assertion, which is
    -- the opposite of what this UI is for.
    turn_id     TEXT,
    turn_index  INTEGER,
    source      TEXT NOT NULL DEFAULT '',
    -- figure pins only: identifies the chart within its turn.
    specialist  TEXT,
    topic       TEXT,
    chart_url   TEXT,
    -- The Vega-Lite spec, JSON-encoded. The REAL durable copy of a figure:
    -- it carries its own data inline, so it survives the chart PNG being
    -- deleted (which happens on rewind, and which the in-turn view never
    -- noticed because it renders from the spec too).
    vega_spec   TEXT,
    -- ChartInfo.kind (trend | bar | share | trend_dual | trend_grid | table).
    -- Drives the card's type glyph, which replaced a thumbnail-sized render
    -- of the chart that conveyed nothing at that size.
    chart_kind  TEXT,
    -- Which report section this pin has been inserted into, if any.
    section_key TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pins_by_case ON pins(case_id, created_at);

CREATE TABLE IF NOT EXISTS opportunities (
    opp_id      TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    -- JSON array of pin_ids this opportunity was synthesised from, so the
    -- "from 2 pins" provenance chip can resolve back to real pins.
    pin_ids     TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS opps_by_case ON opportunities(case_id, created_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
    column never appears without this — pins written before `vega_spec`
    existed live in databases that still lack it.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pins)")}
    if not cols:
        return
    for name, decl in (("vega_spec", "TEXT"), ("chart_kind", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE pins ADD COLUMN {name} {decl}")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # `check_same_thread=False` is safe here because every call opens its own
    # connection and closes it; nothing is shared across threads.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL so a reader on one request never blocks a writer on another.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _row_to_pin(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "pin_id": row["pin_id"],
        "kind": row["kind"],
        "text": row["text"],
        "turn_id": row["turn_id"],
        "turn_index": row["turn_index"],
        "source": row["source"],
        "specialist": row["specialist"],
        "topic": row["topic"],
        "chart_url": row["chart_url"],
        "vega_spec": json.loads(row["vega_spec"]) if row["vega_spec"] else None,
        "chart_kind": row["chart_kind"],
        "section_key": row["section_key"],
        "created_at": row["created_at"],
    }


def list_pins(case_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pins WHERE case_id = ? ORDER BY created_at", (case_id,)
        ).fetchall()
    return [_row_to_pin(r) for r in rows]


def add_pin(case_id: str, *, kind: str, text: str = "", turn_id: str | None = None,
            turn_index: int | None = None, source: str = "",
            specialist: str | None = None, topic: str | None = None,
            chart_url: str | None = None,
            vega_spec: Any = None,
            chart_kind: str | None = None) -> dict[str, Any]:
    """Pin one insight or figure. Returns the stored pin.

    Figure pins are idempotent on ``(case, turn, specialist, topic)``: the
    design's "Pin Figures" button pins every figure on a turn at once, so
    clicking it twice must not produce duplicate cards. Insight pins carry no
    such key — two different sentences from one turn are two real pins.
    """
    if kind not in PIN_KINDS:
        raise ValueError(f"unknown pin kind {kind!r}; expected one of {PIN_KINDS}")

    with _connect() as conn:
        if kind == "figure" and turn_id and topic:
            existing = conn.execute(
                "SELECT * FROM pins WHERE case_id = ? AND kind = 'figure' "
                "AND turn_id = ? AND specialist IS ? AND topic = ?",
                (case_id, turn_id, specialist, topic),
            ).fetchone()
            if existing is not None:
                return _row_to_pin(existing)

        pin_id = uuid.uuid4().hex[:12]
        created = time.time()
        conn.execute(
            "INSERT INTO pins (pin_id, case_id, kind, text, turn_id, turn_index, "
            "source, specialist, topic, chart_url, vega_spec, chart_kind, "
            "section_key, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (pin_id, case_id, kind, text, turn_id, turn_index, source,
             specialist, topic, chart_url,
             json.dumps(vega_spec) if vega_spec else None, chart_kind, created),
        )
        row = conn.execute("SELECT * FROM pins WHERE pin_id = ?", (pin_id,)).fetchone()
    return _row_to_pin(row)


def delete_pin(case_id: str, pin_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM pins WHERE case_id = ? AND pin_id = ?", (case_id, pin_id)
        )
    return cur.rowcount > 0


def set_pin_section(case_id: str, pin_id: str, section_key: str | None) -> bool:
    """Insert a pin into a report section (or, with None, take it back out).

    Stored as a pointer on the pin rather than by editing the section's `.md`:
    the markdown IS the report agent's source, and a UI that rewrites it
    changes what the agent reads on the next turn. The Report panel merges
    these in at render time instead.
    """
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE pins SET section_key = ? WHERE case_id = ? AND pin_id = ?",
            (section_key, case_id, pin_id),
        )
    return cur.rowcount > 0


def pins_by_section(case_id: str) -> dict[str, list[dict[str, Any]]]:
    """Pins grouped by the report section they were inserted into."""
    out: dict[str, list[dict[str, Any]]] = {}
    for pin in list_pins(case_id):
        key = pin["section_key"]
        if key:
            out.setdefault(key, []).append(pin)
    return out


def list_opportunities(case_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM opportunities WHERE case_id = ? ORDER BY created_at",
            (case_id,),
        ).fetchall()
    return [{
        "opp_id": r["opp_id"],
        "title": r["title"],
        "body": r["body"],
        "pin_ids": json.loads(r["pin_ids"]),
        "created_at": r["created_at"],
    } for r in rows]


def add_opportunity(case_id: str, *, title: str, body: str = "",
                    pin_ids: Iterable[str] = ()) -> dict[str, Any]:
    opp_id = uuid.uuid4().hex[:12]
    created = time.time()
    ids = list(pin_ids)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO opportunities (opp_id, case_id, title, body, pin_ids, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (opp_id, case_id, title, body, json.dumps(ids), created),
        )
    return {"opp_id": opp_id, "title": title, "body": body,
            "pin_ids": ids, "created_at": created}


def delete_opportunity(case_id: str, opp_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM opportunities WHERE case_id = ? AND opp_id = ?",
            (case_id, opp_id),
        )
    return cur.rowcount > 0
