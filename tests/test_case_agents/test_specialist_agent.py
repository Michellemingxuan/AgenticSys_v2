from agents import Agent
from case_agents.specialist_agent import build_specialist_agent
from models.types import DomainSkill, SpecialistOutput


def test_build_specialist_agent_returns_agent():
    skill = DomainSkill(
        name="creditrisk",
        system_prompt="You analyze credit risk.",
        data_hints=["bureau", "model_scores"],
        interpretation_guide="Use FICO < 580 as risky.",
        risk_signals=["delinquency", "high DTI"],
    )
    pillar = {"focus": "credit", "cut_off_date": "2025-12-01"}
    agent = build_specialist_agent(skill, pillar, model=None)

    assert isinstance(agent, Agent)
    assert agent.name == "creditrisk"
    assert agent.output_type.output_type is SpecialistOutput
    assert "You analyze credit risk." in agent.instructions
    assert "2025-12-01" in agent.instructions  # pillar overlay rendered
    assert len(agent.tools) == 3   # list_available_tables, get_table_schema, query_table
