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
