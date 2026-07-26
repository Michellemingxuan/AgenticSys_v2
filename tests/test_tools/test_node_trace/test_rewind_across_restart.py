# tests/test_tools/test_node_trace/test_rewind_across_restart.py
from tools.node_trace.core import NodeTraceStore


def _ids(store, conv):
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    return sorted(r[0] for r in conn.execute(
        "SELECT turn_id FROM node_trace WHERE conversation_id = ?", (conv,)))


def test_rewind_pre_restart_turn_survives_restart(tmp_path):
    db = str(tmp_path / "traces.db")
    conv = "366::u::credit_risk"

    # --- server run A: two turns + an end-of-turn snapshot ---
    store_a = NodeTraceStore(db)
    for turn in ("T1", "T2"):
        store_a.insert(chat_id=conv, case_id="366", turn_id=turn, node="root",
                       parent_id=None, depth=0,
                       started_at=f"2026-07-26T00:00:0{turn[-1]}+00:00",
                       conversation_id=conv, server_run_id="run-A",
                       user_id="u", pillar_id="credit_risk")
    store_a.snapshot_session(
        chat_id=conv, case_id="366", turn_id="T2",
        qa_cache={"q": {"turn_id_origin": "T2", "turn_seq": 2}},
        specialist_kb={}, input_history=[],
        conversation_id=conv, server_run_id="run-A",
        user_id="u", pillar_id="credit_risk")
    assert _ids(store_a, conv) == ["T1", "T2"]

    # --- simulate restart: a NEW store on the SAME db (run-B) ---
    store_b = NodeTraceStore(db)
    snap = store_b.load_latest_snapshot("366")
    assert snap is not None and snap["chat_id"] == conv   # restore is case-keyed

    # --- rewind pre-restart turn T1 (delete_turns keys on stable turn_id) ---
    removed = store_b.delete_turns(["T1"])
    assert removed == 1
    assert _ids(store_b, conv) == ["T2"]                  # T2 survives, same conv
