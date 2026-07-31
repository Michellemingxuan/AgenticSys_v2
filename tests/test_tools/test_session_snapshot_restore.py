import os, tempfile
from tools.node_trace.core import NodeTraceStore


def _store():
    d = tempfile.mkdtemp()
    return NodeTraceStore(os.path.join(d, "t.db"))


def test_snapshot_persists_raw_qa_cache_and_loads_latest():
    s = _store()
    qa = {"why held?": {"answer": "FICO.", "turn_id_origin": "t1",
                        "origin_question": "Why held?", "turn_seq": 1}}
    kb = {"risk": [{"topic": "fico", "claim": "low", "captured_at_turn": "t1"}]}
    s.snapshot_session(chat_id="c-1", case_id="CASE", turn_id="t1",
                       qa_cache=qa, specialist_kb=kb)
    snap = s.load_latest_snapshot("CASE")
    assert snap is not None
    assert snap["qa_cache"] == qa          # RAW dict, not the episodic projection
    assert snap["specialist_kb"] == kb
    assert snap["chat_id"] == "c-1"


def test_load_latest_returns_most_recent():
    s = _store()
    s.snapshot_session(chat_id="c-1", case_id="CASE", turn_id="t1",
                       qa_cache={"a": {"turn_seq": 1}}, specialist_kb={})
    s.snapshot_session(chat_id="c-1", case_id="CASE", turn_id="t2",
                       qa_cache={"a": {"turn_seq": 1}, "b": {"turn_seq": 2}},
                       specialist_kb={})
    snap = s.load_latest_snapshot("CASE")
    assert set(snap["qa_cache"].keys()) == {"a", "b"}   # latest


def test_load_latest_none_when_absent():
    assert _store().load_latest_snapshot("NOPE") is None
