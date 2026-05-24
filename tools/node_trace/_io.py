"""Shared read-side helpers for the node_trace DB. Used by both
turn_report.py and optimization_report.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def open_db(db: Path) -> sqlite3.Connection:
    """Open the trace DB for reading. Idempotently applies the schema
    so a fresh / wiped DB returns empty results instead of crashing
    every route with ``no such table: node_trace``."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        # Schema uses CREATE …IF NOT EXISTS everywhere — safe to run on
        # an existing DB. Necessary when the viewer is opened before the
        # server has written anything, or right after rm-ing the DB.
        from tools.node_trace.core import _SCHEMA
        conn.executescript(_SCHEMA)
    except sqlite3.OperationalError:
        # WAL recovery failure on a cloud-synced FS, or a corrupted DB —
        # let the routes show their "no rows" empty state rather than
        # 500ing.
        pass
    return conn
