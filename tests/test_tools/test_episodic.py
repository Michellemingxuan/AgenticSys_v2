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


def test_parse_sub_answer_accepts_dict_payload():
    # The LIVE format: payload is already a dict (SpecialistOutput), not a JSON
    # string. This is what specialists actually store — the old str-only guard
    # silently dropped every sub-answer, so specialists got no episodic.
    assert _parse_sub_answer({"domain": "modeling", "findings": "TSR 39.6"}) == "TSR 39.6"
    assert _parse_sub_answer({"answer": "elevated external delinquency"}) == \
        "elevated external delinquency"
    assert _parse_sub_answer({"domain": "x"}) is None            # neither field
    assert _parse_sub_answer({}) is None


def test_build_records_populates_sub_answers_from_dict_payloads():
    qa = {"q": _entry(1, "Q", [_call("bureau", "sq", {"findings": "FICO 703 in Sep"})])}
    recs = build_records(qa)
    assert recs[0]["sub_answers"] == [
        {"specialist": "bureau", "sub_question": "sq", "sub_answer": "FICO 703 in Sep"}]
    assert select_specialist_episodic(recs, "bureau", k=3) == [
        {"sub_question": "sq", "sub_answer": "FICO 703 in Sep"}]


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


# ── degraded sub-answers must not become next-turn context ──────────────────
#
# agent_tool quarantines a specialist whose answer rested on a FAILED tool call;
# conductor projects that onto the cached tool_call as `degraded: True`. If these
# still reached episodic, one broken tool call would ground every later turn —
# the exact propagation this whole path exists to stop.

def _degraded_call(tool, subq, payload):
    return {**_call(tool, subq, payload), "degraded": True}


def test_build_records_drops_degraded_sub_answers():
    recs = build_records({"k": _entry(1, "Q", [
        _degraded_call("modeling", "trend?", {"findings": "TSR fell to 12.0"}),
        _call("bureau", "fico?", {"findings": "FICO 712"}),
    ])})
    subs = recs[0]["sub_answers"]
    assert [s["specialist"] for s in subs] == ["bureau"]
    assert "12.0" not in str(subs)


def test_build_records_keeps_explicitly_ungraded_sub_answers():
    """`degraded: False` and a missing key both mean 'grounded' — entries cached
    before this field existed must not be dropped wholesale."""
    recs = build_records({"k": _entry(1, "Q", [
        {**_call("modeling", "trend?", {"findings": "TSR 39.6"}), "degraded": False},
        _call("bureau", "fico?", {"findings": "FICO 712"}),
    ])})
    assert [s["specialist"] for s in recs[0]["sub_answers"]] == ["modeling", "bureau"]


def test_degraded_specialist_is_invisible_to_its_own_episodic():
    """The specialist itself must not read back its own ungrounded answer."""
    recs = build_records({"k": _entry(1, "Q", [
        _degraded_call("modeling", "trend?", {"findings": "TSR fell to 12.0"}),
    ])})
    assert select_specialist_episodic(recs, "modeling", k=5) == []


def test_partial_answer_is_stamped_on_the_record_only_when_set():
    """The orchestrator-error fallback stores its turn so the exchange is
    REMEMBERED, but the answer was assembled after synthesis failed. The record
    has to say so, or the next turn builds on a partial result as if it were a
    clean one. Ordinary records stay lean (no key at all)."""
    ok = _entry(1, "Q1", [])
    broken = {**_entry(2, "Q2", []), "partial_answer": True, "no_replay": True}
    recs = build_records({"q1": ok, "q2": broken})

    assert recs[0]["question"] == "Q2"
    assert recs[0]["partial_answer"] is True
    assert "partial_answer" not in recs[1]
    # The instruction the orchestrator reads must explain what the flag means.
    assert "partial_answer" in render_orchestrator_block(recs)
