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
