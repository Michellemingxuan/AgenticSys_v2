import server


def test_history_messages_reconstructs_ordered_pairs():
    qa = {
        "k2": {"origin_question": "Second?", "answer": "A2",
               "turn_id_origin": "t2", "turn_seq": 2},
        "k1": {"origin_question": "First?", "answer": "A1",
               "turn_id_origin": "t1", "turn_seq": 1},
    }
    msgs = server._history_messages(qa)
    # ordered by turn_seq; reviewer then agent per turn
    assert [m["text"] for m in msgs] == ["First?", "A1", "Second?", "A2"]
    assert msgs[0]["role"] == "reviewer" and msgs[1]["role"] == "agent"
    assert msgs[0]["turn_id"] == "t1" and msgs[1]["turn_id"] == "t1"
    # ids are deterministic and dedup-friendly
    assert msgs[1]["id"] == "hist:t1:agent"


def test_history_messages_empty():
    assert server._history_messages({}) == []


# ── timestamps ─────────────────────────────────────────────────────────────
# The thread used to come back undated, so the UI showed a clock only on turns
# asked in that browser since the last history load. Two sources now fill it:
# node_trace (accurate, survives the session) and the entry's own `asked_at`.

def _qa(**over):
    base = {"origin_question": "Q?", "answer": "A", "turn_id_origin": "t1",
            "turn_seq": 1}
    base.update(over)
    return {"k1": base}


def test_node_trace_time_is_sent_in_milliseconds():
    """The browser formats with `new Date(ts)`, which wants ms — seconds
    would date every turn to 1970."""
    msgs = server._history_messages(_qa(), {"t1": 1_700_000_000.5})
    assert [m["timestamp"] for m in msgs] == [1_700_000_000_500] * 2


def test_falls_back_to_the_entrys_own_asked_at():
    """Covers turns node_trace never saw — NODE_TRACE_DISABLE=1, or rows
    since purged."""
    msgs = server._history_messages(_qa(asked_at=1_700_000_000.0), {})
    assert msgs[0]["timestamp"] == 1_700_000_000_000


def test_node_trace_wins_over_asked_at():
    """`asked_at` is written at turn COMPLETION when no start is passed, so
    it can run a turn-duration late; the trace's is the real ask time."""
    msgs = server._history_messages(
        _qa(asked_at=1_700_000_042.0), {"t1": 1_700_000_000.0})
    assert msgs[0]["timestamp"] == 1_700_000_000_000


def test_no_known_time_omits_the_field_entirely():
    """Not a zero: the UI renders nothing for a missing timestamp, but would
    happily format 0 as a real clock time."""
    msgs = server._history_messages(_qa(), {})
    assert all("timestamp" not in m for m in msgs)


def test_a_junk_time_is_treated_as_unknown():
    for bad in (None, 0, -1, "2026-05-21", float("nan")):
        msgs = server._history_messages(_qa(), {"t1": bad})
        assert all("timestamp" not in m for m in msgs), bad


def test_each_turn_gets_its_own_time():
    qa = {
        "k1": {"origin_question": "First?", "answer": "A1",
               "turn_id_origin": "t1", "turn_seq": 1},
        "k2": {"origin_question": "Second?", "answer": "A2",
               "turn_id_origin": "t2", "turn_seq": 2},
    }
    msgs = server._history_messages(
        qa, {"t1": 1_700_000_000.0, "t2": 1_700_000_600.0})
    assert [m["timestamp"] for m in msgs] == [
        1_700_000_000_000, 1_700_000_000_000,
        1_700_000_600_000, 1_700_000_600_000,
    ]


def test_omitting_turn_times_still_works():
    """The signature stayed backward-compatible: existing callers and tests
    pass only the cache."""
    msgs = server._history_messages(_qa(asked_at=1_700_000_000.0))
    assert msgs[0]["timestamp"] == 1_700_000_000_000
