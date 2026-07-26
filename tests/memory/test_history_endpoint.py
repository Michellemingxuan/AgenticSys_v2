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
