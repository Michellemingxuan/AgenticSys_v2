import types
from runner.turn.input_assembly import assemble_orchestrator_input, _compose_framed_question


def _fake_sess(qa_cache=None, specialist_kb=None):
    return types.SimpleNamespace(
        qa_cache=qa_cache or {}, specialist_kb=specialist_kb or {},
        logger=types.SimpleNamespace(log=lambda *a, **k: None))


def test_compose_order_and_skip_empties():
    assert _compose_framed_question("", "", "q?") == "q?"
    assert _compose_framed_question("EP", "KB", "q?") == "EP\n\nKB\n\nq?"


def test_assemble_cold_turn_is_bare_question():
    sess = _fake_sess()
    ctx = types.SimpleNamespace(_turn_id="t1")
    verdict = types.SimpleNamespace(redacted_question="why default?")
    out = assemble_orchestrator_input(sess, verdict, ctx)
    assert out == "why default?"
    assert ctx._episodic_records == []
