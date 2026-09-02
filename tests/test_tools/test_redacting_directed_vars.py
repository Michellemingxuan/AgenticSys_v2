from agent_factories.agent_tools.specialist_input_tool import _compose_specialist_input, _render_directed_variables

def test_compose_no_regression_without_directed_block():
    # byte-identical to the 3-arg behavior when directed_block omitted
    out = _compose_specialist_input("EPI", "KP", "the question")
    assert out == "EPI\n\nKP\n\n--- New question ---\nthe question"

def test_compose_places_directed_block_last():
    out = _compose_specialist_input("EPI", "KP", "the question", "DIR")
    assert out == "EPI\n\nKP\n\nDIR\n\n--- New question ---\nthe question"

def test_compose_directed_only():
    out = _compose_specialist_input("", "", "q", "DIR")
    assert out == "DIR\n\n--- New question ---\nq"

def test_render_directed_variables_format():
    vars = [
        {"concept": "oop", "name": "cust_expsr_avg_rem_12m_ratio",
         "description_short": "Exposure / avg remit", "threshold_text": "risky > 3.15"},
        {"concept": "oop", "name": "oop_interaction",
         "description_short": "OOP spend index", "threshold_text": ""},
    ]
    block = _render_directed_variables(vars)
    assert block.splitlines()[0].startswith("§ DIRECTED VARIABLES")
    assert "[oop] cust_expsr_avg_rem_12m_ratio — Exposure / avg remit; risky > 3.15" in block
    assert "[oop] oop_interaction — OOP spend index" in block

def test_render_empty_is_blank():
    assert _render_directed_variables([]) == ""


def test_render_directed_variables_caps_per_concept_and_frames_directional():
    """Concepts are DIRECTIONS, not a checklist: at most N columns per concept
    plus a directional caveat, so the specialist doesn't trend every matched
    column (the round-count regression fix)."""
    from agent_factories.agent_tools.specialist_input_tool import (
        _DIRECTED_VARS_PER_CONCEPT,
    )
    vars = [{"concept": "spend_pattern", "name": f"sp_{i}",
             "description_short": f"d{i}", "threshold_text": ""} for i in range(5)]
    vars += [{"concept": "output_score", "name": f"os_{i}",
              "description_short": f"e{i}", "threshold_text": ""} for i in range(3)]
    block = _render_directed_variables(vars)
    # framed as directions, not a to-do list
    assert "NOT a checklist" in block
    # capped per concept — not all 5 / all 3 columns
    assert block.count("[spend_pattern]") == _DIRECTED_VARS_PER_CONCEPT
    assert block.count("[output_score]") == _DIRECTED_VARS_PER_CONCEPT
