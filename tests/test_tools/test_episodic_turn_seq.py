from runner.turn import cache


def _mk_session():
    # Minimal CaseSession-like object for _store_cached_qa (which only touches
    # sess.qa_cache and the turn_seq counter). Use the real class if constructible;
    # else a SimpleNamespace with the needed attrs.
    import types
    return types.SimpleNamespace(qa_cache={}, _qa_turn_seq=0)


def test_store_stamps_increasing_turn_seq():
    sess = _mk_session()
    cache._store_cached_qa(sess, "q1", {"answer": "a1"})
    cache._store_cached_qa(sess, "q2", {"answer": "a2"})
    seqs = [sess.qa_cache["q1"]["turn_seq"], sess.qa_cache["q2"]["turn_seq"]]
    assert seqs[0] < seqs[1]                      # strictly increasing
