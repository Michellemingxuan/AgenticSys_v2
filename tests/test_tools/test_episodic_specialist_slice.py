from tools.redacting_tool import _compose_specialist_input
from tools.episodic import select_specialist_episodic, render_specialist_block


def test_compose_preserves_new_question_marker_and_order():
    out = _compose_specialist_input("[EP mine]", "[KB digest]", "sub-q?")
    assert out == "[EP mine]\n\n[KB digest]\n\n--- New question ---\nsub-q?"
    # KB-only (no episodic) must be BYTE-IDENTICAL to the prior format:
    assert _compose_specialist_input("", "[KB digest]", "sub-q?") == \
        "[KB digest]\n\n--- New question ---\nsub-q?"
    # No preface at all → just the question (unchanged):
    assert _compose_specialist_input("", "", "sub-q?") == "sub-q?"


def test_specialist_slice_is_own_history_only():
    records = [
        {"sub_answers": [{"specialist": "spend_payments", "sub_question": "a", "sub_answer": "sa"}]},
        {"sub_answers": [{"specialist": "modeling", "sub_question": "c", "sub_answer": "CDSS 2024-11"}]},
    ]
    block = render_specialist_block(select_specialist_episodic(records, "modeling", 3))
    assert "CDSS 2024-11" in block and "spend_payments" not in block
