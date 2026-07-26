import sqlite3

from tools.node_trace.core import NodeTraceStore


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_new_columns_exist_on_fresh_db(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    conn = sqlite3.connect(store.db_path)
    for table in ("node_trace", "session_snapshot"):
        cols = _cols(conn, table)
        assert {"conversation_id", "server_run_id", "user_id", "pillar_id"} <= cols


def test_insert_writes_identity_columns(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    rid = store.insert(
        chat_id="conv-x", case_id="366", turn_id="T1", node="root",
        parent_id=None, depth=0, started_at="2026-07-26T00:00:00+00:00",
        conversation_id="conv-x", server_run_id="run-A",
        user_id="amx_reviewer", pillar_id="credit_risk",
    )
    assert rid > 0
    conn = sqlite3.connect(store.db_path)
    row = conn.execute(
        "SELECT conversation_id, server_run_id, user_id, pillar_id, chat_id "
        "FROM node_trace WHERE id = ?", (rid,)).fetchone()
    assert row == ("conv-x", "run-A", "amx_reviewer", "credit_risk", "conv-x")


def test_migration_adds_columns_to_legacy_db(tmp_path):
    # Build a "legacy" node_trace/session_snapshot WITHOUT the new columns.
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE node_trace (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id TEXT NOT NULL, case_id TEXT NOT NULL, turn_id TEXT NOT NULL,"
        " node TEXT NOT NULL, parent_id INTEGER, depth INTEGER NOT NULL,"
        " started_at TEXT NOT NULL);"
        "INSERT INTO node_trace (chat_id, case_id, turn_id, node, depth, started_at)"
        " VALUES ('old-chat','366','T0','root',0,'2026-07-01T00:00:00+00:00');"
        "CREATE TABLE session_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id TEXT NOT NULL, case_id TEXT NOT NULL, turn_id TEXT NOT NULL,"
        " taken_at TEXT NOT NULL);")
    conn.commit()
    conn.close()

    # Reopening through NodeTraceStore must migrate in the new columns and
    # preserve the legacy row (readable via COALESCE(conversation_id, chat_id)).
    store = NodeTraceStore(db)
    conn = sqlite3.connect(store.db_path)
    assert {"conversation_id", "server_run_id", "user_id", "pillar_id"} <= _cols(conn, "node_trace")
    assert {"conversation_id", "server_run_id", "user_id", "pillar_id"} <= _cols(conn, "session_snapshot")
    grp = conn.execute(
        "SELECT COALESCE(conversation_id, chat_id) FROM node_trace").fetchone()[0]
    assert grp == "old-chat"
