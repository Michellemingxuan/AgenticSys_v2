from agents import Agent
from agent_factories.general_specialist import build_general_specialist
from models.types import ReviewReport


def test_build_general_specialist_returns_agent():
    agent = build_general_specialist(model=None)
    assert isinstance(agent, Agent)
    assert agent.name == "general_specialist"
    assert agent.output_type.output_type is ReviewReport
    # general_specialist now gets exactly one tool — `make_chart` — for
    # producing cross-domain comparison charts (overlay two specialists'
    # series on the same axis). It does NOT have data-query tools because
    # comparison.md forbids introducing new factual claims.
    assert [t.name for t in agent.tools] == ["make_chart"]
    assert "compare" in agent.instructions.lower() or "contradiction" in agent.instructions.lower()
