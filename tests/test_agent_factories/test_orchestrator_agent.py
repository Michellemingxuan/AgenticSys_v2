from agents import Agent
from agent_factories.orchestrator_agent import build_orchestrator_agent
from agent_factories.specialist_agent import build_specialist_agent
from agent_factories.report_agent import build_report_agent
from models.types import DomainSkill, FinalAnswer


def _build_two_specialist_orchestrator():
    skill_a = DomainSkill(name="creditrisk", system_prompt="x", data_hints=[],
                          interpretation_guide="", risk_signals=[])
    skill_b = DomainSkill(name="taxcompliance", system_prompt="y", data_hints=[],
                          interpretation_guide="", risk_signals=[])
    specialists = [build_specialist_agent(skill_a, {}, model=None),
                   build_specialist_agent(skill_b, {}, model=None)]
    report = build_report_agent(model=None)
    return build_orchestrator_agent(specialists, report, model=None)


def test_build_orchestrator_agent_wires_all_tools():
    agent = _build_two_specialist_orchestrator()

    assert isinstance(agent, Agent)
    assert agent.name == "orchestrator"
    assert agent.output_type.output_type is FinalAnswer
    # 2 specialists + report_agent = 3 tools (NO general_specialist — the
    # coherence review is enforced server-side, not self-called here).
    assert len(agent.tools) == 3
    # Instructions is now a dynamic callable — test that it returns a string
    assert callable(agent.instructions)
    from unittest.mock import MagicMock
    mock_ctx = MagicMock()
    mock_ctx.context = MagicMock()
    mock_ctx.context._domain_specialists_called = set()
    prompt = agent.instructions(mock_ctx, agent)
    for keyword in ["specialist", "synthes"]:
        assert keyword in prompt.lower()


def test_orchestrator_agent_has_no_general_specialist_tool():
    """Double-review removed: the orchestrator can no longer self-call the
    reviewer. Its tool set must not include `general_specialist`; review is
    enforced once, server-side."""
    agent = _build_two_specialist_orchestrator()
    tool_names = {t.name for t in agent.tools}
    assert "general_specialist" not in tool_names, tool_names
    assert "report_agent" in tool_names


def test_orchestrator_prompt_does_not_instruct_calling_general_specialist():
    """The R1 prompt must not tell the model to call `general_specialist`
    (the old ★ general_specialist rules ★ block)."""
    from unittest.mock import MagicMock
    agent = _build_two_specialist_orchestrator()
    mock_ctx = MagicMock()
    mock_ctx.context = MagicMock()
    mock_ctx.context._domain_specialists_called = set()
    prompt = agent.instructions(mock_ctx, agent)
    assert "★ `general_specialist` rules ★" not in prompt
    assert "call `general_specialist`" not in prompt
    # Server-side review is now called out in the protocol.
    assert "SERVER-SIDE" in prompt


def test_build_general_specialist_still_importable():
    """The server's coherence reviewer must remain importable/constructible
    even though the orchestrator no longer wires it as a tool."""
    from agent_factories.general_specialist import build_general_specialist
    reviewer = build_general_specialist(model=None)
    assert reviewer is not None


def test_report_only_carveout_is_narrow_and_defaults_to_dispatch():
    """The carve-out lets a question ABOUT the report skip specialists, but it
    must stay narrow: four runs of "summarize the spending patterns from the
    report" split two report-only / two data-only, because the prompt demanded
    BOTH and the model resolved the contradiction differently each time."""
    from agent_factories.orchestrator_agent import _compose_orchestrator_instructions

    p = _compose_orchestrator_instructions()

    # The default is unchanged: both are still mandatory.
    assert "MUST have called BOTH (1) report_agent and (2) at least one domain specialist" in p
    # The exception exists, is flagged as narrow, and is scoped to the report
    # being the SUBJECT.
    assert "ONE EXCEPTION, and it is NARROW" in p
    assert "SUBJECT of the question" in p
    # It carries a decision test and a default, so an ambiguous question
    # dispatches rather than silently answering from report narrative.
    assert "When in DOUBT, dispatch" in p
    assert "unanswerable" in p
    # And it must be attributed, so the reviewer knows the source.
    assert "the curated reports state" in p
    # Worked examples on BOTH sides — the qualifying and the disqualifying.
    assert "FROM THE REPORT" in p
    assert "what is the spending pattern" in p


def test_every_report_sourced_sentence_is_attributed():
    """A reviewer must be able to tell, sentence by sentence, what was measured
    THIS RUN from what a curated report asserted earlier. Path A prefixes the
    answer, a contradiction names the report while overruling it, and the
    report-only carve-out attributes explicitly — Path B was the one hole,
    where report narrative continued the specialist's paragraph unmarked."""
    from agent_factories.orchestrator_agent import _compose_orchestrator_instructions

    p = _compose_orchestrator_instructions()

    # Path B now requires the source to be named, with usable openers.
    assert "MUST NAME THE REPORT AS ITS SOURCE" in p
    assert "prior reports also note" in p.lower()
    # And the JSON template models it, so the shape is copyable.
    assert "Prior reports also note <1-sentence report context>" in p
    # The case-overview heading carries its own attribution.
    assert "Key Risks (from prior curated reports)" in p
    # The pre-existing attributions are untouched.
    assert "No prior curated reports — answer is from live specialist analysis only." in p
    assert "Report-vs-data disagreement" in p
