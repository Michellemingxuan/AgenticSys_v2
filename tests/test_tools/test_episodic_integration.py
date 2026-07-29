"""Integration: the coreference scenario the episodic tier exists for.

Prior turn "How did CDSS react?" (answered by modeling) must give the
orchestrator + the modeling specialist enough context to resolve a follow-up
like "when did it reach the second spike?"."""
from tools.episodic import (
    build_records, select_episodic, render_orchestrator_block,
    select_specialist_episodic, render_specialist_block,
)


def test_cdss_followup_has_prior_context():
    qa_cache = {
        "how did cdss react?": {
            "turn_seq": 1, "turn_id_origin": "t1",
            "origin_question": "How did CDSS react?",
            "answer": "CDSS rose through 2024, spiking at 2024-06 and 2024-11.",
            "tool_calls": [{
                "call_id": "c1", "tool": "modeling",
                "sub_question": "CDSS trajectory + drivers",
                "payload": '{"domain":"modeling","findings":"CDSS spiked at '
                           '2024-06 and 2024-11.","evidence":[1]}',
            }],
        },
    }
    records = build_records(qa_cache)

    # Orchestrator sees the prior turn → can resolve "it" and "the second spike".
    orch = render_orchestrator_block(select_episodic(records, 3))
    assert "CDSS" in orch and "2024-11" in orch and "How did CDSS react?" in orch

    # modeling, re-invoked, sees its OWN prior CDSS answer.
    mine = render_specialist_block(select_specialist_episodic(records, "modeling", 3))
    assert "2024-11" in mine


def test_degraded_turn_does_not_ground_the_followup():
    """The propagation this path exists to stop, end to end.

    Turn 1's modeling answer rested on a tool call that failed and never
    recovered, so agent_tool quarantined it. The follow-up turn must NOT see
    those numbers — not in the orchestrator's episodic block, and not in
    modeling's own. The bureau specialist, which ran fine, is unaffected.
    """
    from runner.turn.conductor import _cacheable_tool_calls

    # What the conductor writes into qa_cache at end of turn.
    live_tool_calls = [
        {"call_id": "c1", "tool": "modeling", "duration_ms": 900,
         "sub_question": "CDSS trajectory",
         "payload": {"domain": "modeling", "findings": "CDSS fell to 12.0."}},
        {"call_id": "c2", "tool": "bureau", "duration_ms": 400,
         "sub_question": "FICO?",
         "payload": {"domain": "bureau", "findings": "FICO 712 as of 2024-11."}},
    ]
    cached = _cacheable_tool_calls(live_tool_calls, {"modeling"})

    # The payload survives for UI replay...
    assert cached[0]["payload"]["findings"] == "CDSS fell to 12.0."
    assert cached[0]["degraded"] is True and cached[1]["degraded"] is False

    records = build_records({"q": {
        "turn_seq": 1, "turn_id_origin": "t1", "origin_question": "How did CDSS react?",
        "answer": "CDSS fell.", "tool_calls": cached,
    }})

    # ...but never becomes context for the next turn.
    orch = render_orchestrator_block(select_episodic(records, 3))
    assert "12.0" not in orch
    assert "FICO 712" in orch, "a healthy specialist must still be carried forward"
    assert render_specialist_block(
        select_specialist_episodic(records, "modeling", 3)) == ""
