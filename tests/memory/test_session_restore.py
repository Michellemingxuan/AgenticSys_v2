import types
import server


class _FakeStore:
    def __init__(self, snap):
        self._snap = snap
    def load_latest_snapshot(self, case_id):
        return self._snap


def _sess():
    return types.SimpleNamespace(qa_cache={}, specialist_kps={},
                                 _qa_turn_seq=0,
                                 logger=types.SimpleNamespace(log=lambda *a, **k: None))


def test_restore_populates_ram_from_snapshot(monkeypatch):
    snap = {"chat_id": "c-1",
            "qa_cache": {"q1": {"turn_seq": 1}, "q2": {"turn_seq": 2}},
            "specialist_kps": {"risk": [{"topic": "fico"}]}}
    monkeypatch.setattr(server, "_NODE_TRACE_STORE", _FakeStore(snap))
    sess = _sess()
    server._restore_session_state(sess, "CASE")
    assert set(sess.qa_cache) == {"q1", "q2"}
    assert sess.specialist_kps == {"risk": [{"topic": "fico"}]}
    assert sess._qa_turn_seq == 2          # continues from max turn_seq


def test_restore_noop_without_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_NODE_TRACE_STORE", _FakeStore(None))
    sess = _sess()
    server._restore_session_state(sess, "CASE")
    assert sess.qa_cache == {} and sess._qa_turn_seq == 0


def test_restore_noop_without_store(monkeypatch):
    monkeypatch.setattr(server, "_NODE_TRACE_STORE", None)
    sess = _sess()
    server._restore_session_state(sess, "CASE")   # must not raise
    assert sess.qa_cache == {}
