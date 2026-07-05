import server


def test_compose_framed_question_orders_and_skips_empty():
    out = server._compose_framed_question("[EPISODIC ...]", "[KB-warmth ...]", "Q?")
    assert out == "[EPISODIC ...]\n\n[KB-warmth ...]\n\nQ?"
    # empty episodic + empty warmth → just the question
    assert server._compose_framed_question("", "", "Q?") == "Q?"
    # episodic present, warmth empty
    assert server._compose_framed_question("[EP]", "", "Q?") == "[EP]\n\nQ?"
