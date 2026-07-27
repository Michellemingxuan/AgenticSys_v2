"""Orchestrator input composition: case summary (older context, past the
episodic window) + episodic (recent turns) + KB-warmth (topics) + question."""
from types import SimpleNamespace
from runner.turn.input_assembly import assemble_orchestrator_input


def _sess():
    return SimpleNamespace(
        specialist_kb={"risk": [{"topic": "tsr", "claim": "x" * 500}]},
        qa_cache={},
        logger=SimpleNamespace(log=lambda *a, **k: None),
    )


def test_case_summary_block_injected_alongside_warmth():
    sess = _sess()
    verdict = SimpleNamespace(redacted_question="What about TSR?")
    ctx = SimpleNamespace(_turn_id="t1")
    out = assemble_orchestrator_input(
        sess, verdict, ctx, case_summary="Case: TSR peaked at 39.6 in Sep 2024")
    assert "CASE SUMMARY" in out
    assert "Case: TSR peaked at 39.6 in Sep 2024" in out
    assert "KB-warmth" in out                 # warmth is always built now
    assert "What about TSR?" in out


def test_no_case_summary_omits_block():
    sess = _sess()
    verdict = SimpleNamespace(redacted_question="What about TSR?")
    ctx = SimpleNamespace(_turn_id="t1")
    out = assemble_orchestrator_input(sess, verdict, ctx, case_summary="")
    assert "CASE SUMMARY" not in out
    assert "KB-warmth" in out
    assert "What about TSR?" in out
