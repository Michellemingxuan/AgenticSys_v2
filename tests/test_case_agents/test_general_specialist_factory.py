from agents import Agent
from case_agents.general_specialist import build_general_specialist
from models.types import ReviewReport


def test_build_general_specialist_returns_agent():
    agent = build_general_specialist(model=None)
    assert isinstance(agent, Agent)
    assert agent.name == "general_specialist"
    assert agent.output_type is ReviewReport
    assert agent.tools == []
    assert "compare" in agent.instructions.lower() or "contradiction" in agent.instructions.lower()
