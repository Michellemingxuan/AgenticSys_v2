import sqlite3

from tools.node_trace.core import NodeTraceStore
from tools.node_trace import viewer as V


def _seed(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    cid = "366::u::credit_risk"
    # Two turns, two server runs, same conversation.
    for turn, run in (("T1", "run-A"), ("T2", "run-B")):
        store.insert(chat_id=cid, case_id="366", turn_id=turn, node="root",
                     parent_id=None, depth=0,
                     started_at=f"2026-07-26T00:00:0{turn[-1]}+00:00",
                     conversation_id=cid, server_run_id=run,
                     user_id="u", pillar_id="credit_risk")
    return store, cid


def test_index_groups_two_runs_as_one_conversation(tmp_path):
    store, cid = _seed(tmp_path)
    V.app.config["NODE_TRACE_DB"] = store.db_path
    client = V.app.test_client()
    html = client.get("/").get_data(as_text=True)

    # Ground truth via the DB: exactly one session_summary row for this
    # conversation, and it counts both turns.
    conn = sqlite3.connect(store.db_path)
    try:
        rows = conn.execute(
            "SELECT n_turns FROM session_summary WHERE chat_id = ?", (cid,)
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] == 2

        # Both server runs are present under the one grouped conversation —
        # proves the rows genuinely came from two separate server runs
        # rather than one run seeded twice.
        runs = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT server_run_id FROM node_trace "
                "WHERE conversation_id = ?", (cid,)
            ).fetchall()
        }
        assert runs == {"run-A", "run-B"}
    finally:
        conn.close()

    # Render smoke-check: exactly one conversation row is emitted for cid
    # (the index template emits one "/chat/{chat_id}" href per row).
    assert cid in html
    assert html.count(f"/chat/{cid}") == 1


def test_turn_page_shows_server_run_id(tmp_path):
    store, cid = _seed(tmp_path)
    V.app.config["NODE_TRACE_DB"] = store.db_path
    client = V.app.test_client()
    html = client.get(f"/turn/{cid}/T1").get_data(as_text=True)
    assert "run-A" in html          # diagnostic surfaced on the turn view
