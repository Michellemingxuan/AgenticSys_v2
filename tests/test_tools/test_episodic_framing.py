from runner.turn.input_assembly import _compose_framed_question


def test_compose_framed_question_orders_and_skips_empty():
    out = _compose_framed_question("[EPISODIC ...]", "[KB-warmth ...]", "Q?")
    assert out == "[EPISODIC ...]\n\n[KB-warmth ...]\n\nQ?"
    # empty episodic + empty warmth → just the question
    assert _compose_framed_question("", "", "Q?") == "Q?"
    # episodic present, warmth empty
    assert _compose_framed_question("[EP]", "", "Q?") == "[EP]\n\nQ?"
