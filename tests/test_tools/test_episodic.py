# tests/test_tools/test_episodic.py
from tools.episodic import (
    _parse_sub_answer, build_records, select_episodic,
    select_specialist_episodic, render_orchestrator_block, render_specialist_block,
)


def _entry(seq, q, calls, answer="A"):
    return {"turn_seq": seq, "turn_id_origin": f"t{seq}", "origin_question": q,
            "answer": answer, "tool_calls": calls}


def _call(tool, subq, payload):
    return {"call_id": f"c-{tool}", "tool": tool, "sub_question": subq, "payload": payload}


def test_parse_sub_answer_findings_and_prefix():
    p = '[Sub-question: x]\n{"domain":"modeling","findings":"TSR 39.6","evidence":[1]}'
    assert _parse_sub_answer(p) == "TSR 39.6"


def test_parse_sub_answer_falls_back_to_answer_for_report_agent():
    p = '{"coverage":"implicit","answer":"elevated external delinquency"}'
    assert _parse_sub_answer(p) == "elevated external delinquency"


def test_parse_sub_answer_skips_non_json_and_failed():
    assert _parse_sub_answer("[FAILED modeling] timeout: ...") is None
    assert _parse_sub_answer("just prose") is None
    assert _parse_sub_answer('{"domain":"x"}') is None          # neither field
    assert _parse_sub_answer(None) is None


def test_parse_sub_answer_truncates(monkeypatch):
    import tools.episodic as ep
    monkeypatch.setattr(ep, "EPISODIC_SUBANSWER_CHARS", 5)
    assert ep._parse_sub_answer('{"findings":"abcdefgh"}') == "abcde"


def test_build_records_orders_by_turn_seq_desc_not_dict_order():
    qa = {  # dict insertion order is 1,2,3 but turn_seq says 3 is newest
        "q1": _entry(1, "Q1", [_call("modeling", "sq1", '{"findings":"f1"}')]),
        "q3": _entry(3, "Q3", [_call("modeling", "sq3", '{"findings":"f3"}')]),
        "q2": _entry(2, "Q2", [_call("spend_payments", "sq2", '{"findings":"f2"}')]),
    }
    recs = build_records(qa)
    assert [r["question"] for r in recs] == ["Q3", "Q2", "Q1"]
    assert recs[0]["sub_answers"][0] == {
        "specialist": "modeling", "sub_question": "sq3", "sub_answer": "f3"}


def test_build_records_final_answer_truncates(monkeypatch):
    import tools.episodic as ep
    monkeypatch.setattr(ep, "EPISODIC_ANSWER_CHARS", 4)
    recs = ep.build_records({"q": _entry(1, "Q", [], answer="abcdefg")})
    assert recs[0]["final_answer"] == "abcd"


def test_build_records_window_and_bad_subanswers():
    qa = {f"q{i}": _entry(i, f"Q{i}",
          [_call("modeling", "s", '{"findings":"ok"}'),
           _call("report_agent", "s", "not-json")]) for i in range(1, 16)}
    recs = build_records(qa, window=10)
    assert len(recs) == 10                       # window bound
    assert [sa["specialist"] for sa in recs[0]["sub_answers"]] == ["modeling"]  # bad one skipped
    assert recs[0]["question"] == "Q15"          # newest


def test_select_episodic_takes_first_k():
    recs = [{"question": f"Q{i}"} for i in range(5)]
    assert [r["question"] for r in select_episodic(recs, k=3)] == ["Q0", "Q1", "Q2"]


def test_select_specialist_episodic_own_history_reaches_back():
    # modeling ran in the OLDEST turn only; still returned (not empty).
    recs = [
        {"sub_answers": [{"specialist": "spend_payments", "sub_question": "a", "sub_answer": "sa"}]},
        {"sub_answers": [{"specialist": "spend_payments", "sub_question": "b", "sub_answer": "sb"}]},
        {"sub_answers": [{"specialist": "modeling", "sub_question": "c", "sub_answer": "sc"}]},
    ]
    out = select_specialist_episodic(recs, "modeling", k=3)
    assert out == [{"sub_question": "c", "sub_answer": "sc"}]
    assert select_specialist_episodic(recs, "bureau", k=3) == []


def test_render_blocks_empty_and_nonempty():
    assert render_orchestrator_block([]) == ""
    assert render_specialist_block([]) == ""
    b = render_orchestrator_block([{"question": "How did CDSS react?",
        "sub_answers": [{"specialist": "modeling", "sub_question": "x",
                         "sub_answer": "CDSS spiked 2024-06 and 2024-11"}],
        "final_answer": "CDSS rose..."}])
    assert "EPISODIC" in b and "CDSS" in b and "2024-11" in b
    s = render_specialist_block([{"sub_question": "x", "sub_answer": "CDSS spiked 2024-11"}])
    assert "EPISODIC" in s and "2024-11" in s
