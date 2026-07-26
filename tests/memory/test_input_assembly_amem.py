from types import SimpleNamespace
from runner.turn.input_assembly import assemble_orchestrator_input


def _sess():
    return SimpleNamespace(
        specialist_kb={"risk": [{"topic": "tsr", "claim": "x" * 500}]},
        qa_cache={},
        logger=SimpleNamespace(log=lambda *a, **k: None),
    )


def test_amem_block_replaces_warmth_hint():
    sess = _sess()
    verdict = SimpleNamespace(redacted_question="What about TSR?")
    ctx = SimpleNamespace(_turn_id="t1")
    amem = "[AMEM — relevant prior knowledge for this case:\n  - [conversation] full claim not truncated\n]"
    out = assemble_orchestrator_input(sess, verdict, ctx, amem_block=amem)
    assert "full claim not truncated" in out
    assert "KB-warmth" not in out          # bulk warmth hint suppressed
    assert "What about TSR?" in out


def test_no_amem_block_uses_legacy_warmth():
    sess = _sess()
    verdict = SimpleNamespace(redacted_question="What about TSR?")
    ctx = SimpleNamespace(_turn_id="t1")
    out = assemble_orchestrator_input(sess, verdict, ctx, amem_block="")
    assert "KB-warmth" in out               # legacy path intact
