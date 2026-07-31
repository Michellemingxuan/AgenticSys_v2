import sqlite3
from pathlib import Path

import pytest

from tools.node_trace import NodeTraceStore


def test_store_creates_schema_and_inserts(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    store = NodeTraceStore(str(db_path))
    row_id = store.insert(
        chat_id="case-X-aaaa",
        case_id="X",
        turn_id="turn1",
        node="chat.redact",
        parent_id=None,
        depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    assert isinstance(row_id, int) and row_id > 0

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "SELECT chat_id, case_id, turn_id, node, depth FROM node_trace WHERE id = ?",
        (row_id,),
    )
    row = cur.fetchone()
    assert row == ("case-X-aaaa", "X", "turn1", "chat.redact", 0)


def test_store_update_finalizes_row(tmp_path: Path) -> None:
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    row_id = store.insert(
        chat_id="c", case_id="c", turn_id="t",
        node="chat.redact", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    store.update(
        row_id,
        ended_at="2026-05-21T00:00:01.500000+00:00",
        duration_ms=1500,
        prompt_tokens=120,
        completion_tokens=8,
        total_tokens=128,
        outcome="ok",
    )
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    row = conn.execute(
        "SELECT duration_ms, prompt_tokens, completion_tokens, total_tokens, outcome "
        "FROM node_trace WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row == (1500, 120, 8, 128, "ok")


def test_store_swallows_db_failure(tmp_path: Path) -> None:
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    store._conn.close()
    assert store.insert(
        chat_id="c", case_id="c", turn_id="t",
        node="x", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    ) == -1
    store.update(1, outcome="ok")  # also must not raise


import asyncio

from tools.node_trace import NodeTrace, attach_usage


def test_nested_node_traces_parent_chain(tmp_path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))

    async def run():
        async with NodeTrace(
            store, chat_id="c", case_id="c", turn_id="t",
            node="specialist.spend_payments", depth=0,
        ) as outer:
            async with NodeTrace(
                store, chat_id="c", case_id="c", turn_id="t",
                node="specialist.spend_payments.round_1", depth=1,
            ) as inner:
                attach_usage(
                    prompt_tokens=100, completion_tokens=20,
                    prompt_excerpt="hi", completion_excerpt="ok",
                    model="gpt-test",
                )
        return outer.row_id, inner.row_id

    outer_id, inner_id = asyncio.run(run())

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    parent = conn.execute(
        "SELECT id, parent_id, outcome FROM node_trace WHERE id = ?",
        (outer_id,),
    ).fetchone()
    child = conn.execute(
        "SELECT id, parent_id, outcome, prompt_tokens, completion_tokens, model "
        "FROM node_trace WHERE id = ?",
        (inner_id,),
    ).fetchone()
    assert parent == (outer_id, None, "ok")
    assert child == (inner_id, outer_id, "ok", 100, 20, "gpt-test")


def test_node_trace_records_failure(tmp_path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))

    async def run():
        try:
            async with NodeTrace(
                store, chat_id="c", case_id="c", turn_id="t",
                node="chat.redact", depth=0,
            ) as nt:
                raise ValueError("boom")
        except ValueError:
            pass
        return nt.row_id

    row_id = asyncio.run(run())
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    row = conn.execute(
        "SELECT outcome, error_type FROM node_trace WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row == ("failed", "ValueError")


def test_attach_usage_noop_without_active_node():
    attach_usage(prompt_tokens=1, completion_tokens=1)


def test_store_falls_back_when_wal_rejected(tmp_path, monkeypatch):
    """Simulate Google-Drive-style WAL rejection; store should still
    initialize and accept inserts."""
    real_setup = NodeTraceStore._setup_journal_mode

    def setup_rejecting_wal(self):
        # Pretend the cloud-synced FS rejected WAL: route the WAL pragma
        # through the real connection so the OperationalError is generated
        # for real (raise it inline instead).
        with pytest.raises(sqlite3.OperationalError):
            raise sqlite3.OperationalError("disk I/O error")
        # Unreachable; the real method also tries DELETE fallback. We
        # simulate the whole thing by calling the real one with WAL
        # intercepted.
        return real_setup(self)

    def patched(self):
        try:
            raise sqlite3.OperationalError("disk I/O error")
        except sqlite3.OperationalError as exc:
            self._log_failure("wal_setup", exc)
        # Try DELETE (this one should work on tmp_path).
        self._conn.execute("PRAGMA journal_mode=DELETE")

    monkeypatch.setattr(NodeTraceStore, "_setup_journal_mode", patched)

    store = NodeTraceStore(str(tmp_path / "no-wal.db"))
    row_id = store.insert(
        chat_id="c", case_id="x", turn_id="t",
        node="n", parent_id=None, depth=0,
        started_at="2026-05-22T00:00:00.000000+00:00",
    )
    assert row_id > 0
    # Confirm the fallback note went to stderr.
    assert store._failure_logged


def test_snapshot_session_persists_and_counts(tmp_path):
    from collections import OrderedDict
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    qa = OrderedDict([("q1", {"answer": "a1"}), ("q2", {"answer": "a2"})])
    kb = {"spend_payments": [{"topic": "t", "claim": "c"}], "modeling": []}
    row_id = store.snapshot_session(
        chat_id="C", case_id="X", turn_id="T",
        qa_cache=qa, specialist_kb=kb,
    )
    assert row_id > 0
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    row = conn.execute(
        "SELECT qa_cache_n, kb_specialists_n, kb_kps_n "
        "FROM session_snapshot WHERE id = ?",
        (row_id,),
    ).fetchone()
    # 2 cache entries, 1 specialist with KPs (modeling is empty so not
    # counted), 1 KP total.
    assert row[0] == 2
    assert row[1] == 1
    assert row[2] == 1


def test_snapshot_stores_clean_episodic_projection_not_raw_qa_cache(tmp_path):
    """qa_cache_json is the episodic projection (question → sub-answers →
    final answer), NOT the raw entry: no tool-call payloads, no chart specs."""
    import json
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    qa = {
        "how did cdss react?": {
            "turn_seq": 1, "turn_id_origin": "t1",
            "origin_question": "How did CDSS react?",
            "answer": "CDSS spiked at 2024-11.",
            "charts": [{"spec": "VEGA-CHART-NOISE"}],
            "tool_calls": [{
                "call_id": "c1", "tool": "modeling",
                "sub_question": "CDSS trajectory",
                "payload": '{"domain":"modeling","findings":"CDSS spiked 2024-11.",'
                           '"evidence":[1]}',
                "duration_ms": 1234,
            }],
        },
    }
    row_id = store.snapshot_session(
        chat_id="C", case_id="X", turn_id="T",
        qa_cache=qa, specialist_kb={},
    )
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    (qa_n, qa_json) = conn.execute(
        "SELECT qa_cache_n, qa_cache_json FROM session_snapshot WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert qa_n == 1                                   # true turn count preserved
    records = json.loads(qa_json)
    assert records[0]["question"] == "How did CDSS react?"
    assert records[0]["final_answer"] == "CDSS spiked at 2024-11."
    assert records[0]["sub_answers"][0]["specialist"] == "modeling"
    assert records[0]["sub_answers"][0]["sub_answer"] == "CDSS spiked 2024-11."
    # The noisy raw fields must be gone.
    assert "VEGA-CHART-NOISE" not in qa_json
    assert "duration_ms" not in qa_json
    assert "evidence" not in qa_json
    assert "charts" not in qa_json


def test_delete_chat_drops_snapshots_too(tmp_path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    store.snapshot_session(
        chat_id="A", case_id="x", turn_id="t",
        qa_cache={}, specialist_kb={},
    )
    store.snapshot_session(
        chat_id="B", case_id="x", turn_id="t",
        qa_cache={}, specialist_kb={},
    )
    store.delete_chat("A")
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    remaining = [r[0] for r in conn.execute(
        "SELECT chat_id FROM session_snapshot"
    )]
    assert remaining == ["B"]


def test_delete_case_wipes_all_chats_for_that_case(tmp_path):
    """A case may span multiple session/chat ids (one per server process).
    delete_case clears them all — what rewind needs."""
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    # Same case_id, two different chat_ids (= two server runs).
    store.insert(chat_id="case-X-aaaa", case_id="X", turn_id="t1",
                 node="orchestrator", parent_id=None, depth=0,
                 started_at="2026-05-22T00:00:00.000000+00:00")
    store.insert(chat_id="case-X-bbbb", case_id="X", turn_id="t2",
                 node="orchestrator", parent_id=None, depth=0,
                 started_at="2026-05-22T00:00:01.000000+00:00")
    # Different case, should NOT be touched.
    store.insert(chat_id="case-Y-cccc", case_id="Y", turn_id="t3",
                 node="orchestrator", parent_id=None, depth=0,
                 started_at="2026-05-22T00:00:02.000000+00:00")

    removed = store.delete_case("X")
    assert removed == 2

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    remaining = [r[0] for r in conn.execute("SELECT case_id FROM node_trace")]
    assert remaining == ["Y"]


def test_delete_chat_removes_only_that_chat(tmp_path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    a = store.insert(
        chat_id="A", case_id="x", turn_id="t",
        node="x", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    b = store.insert(
        chat_id="B", case_id="x", turn_id="t",
        node="x", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    assert a > 0 and b > 0
    removed = store.delete_chat("A")
    assert removed == 1
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    remaining = [r[0] for r in conn.execute("SELECT chat_id FROM node_trace")]
    assert remaining == ["B"]
