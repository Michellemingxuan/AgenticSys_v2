from agents import Agent
from case_agents.orchestrator_agent import build_orchestrator_agent
from case_agents.specialist_agent import build_specialist_agent
from case_agents.general_specialist import build_general_specialist
from case_agents.report_agent import build_report_agent
from models.types import DomainSkill, FinalAnswer


def test_build_orchestrator_agent_wires_all_tools():
    skill_a = DomainSkill(name="creditrisk", system_prompt="x", data_hints=[],
                          interpretation_guide="", risk_signals=[])
    skill_b = DomainSkill(name="taxcompliance", system_prompt="y", data_hints=[],
                          interpretation_guide="", risk_signals=[])
    specialists = [build_specialist_agent(skill_a, {}, model=None),
                   build_specialist_agent(skill_b, {}, model=None)]
    report = build_report_agent(model=None)
    general = build_general_specialist(model=None)

    agent = build_orchestrator_agent(specialists, report, general, model=None)

    assert isinstance(agent, Agent)
    assert agent.name == "orchestrator"
    assert agent.output_type.output_type is FinalAnswer
    # 2 specialists + report_agent + general_specialist = 4 tools
    assert len(agent.tools) == 4
    # Instructions absorb the four workflow skills
    for keyword in ["specialist", "synthes", "balanc"]:
        assert keyword in agent.instructions.lower()
