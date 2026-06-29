from agents import Agent
from agent_factories.report_agent import build_report_agent
from models.types import ReportDraft


def test_build_report_agent_returns_agent():
    agent = build_report_agent(model=None)
    assert isinstance(agent, Agent)
    assert agent.name == "report_agent"
    assert agent.output_type.output_type is ReportDraft
    assert len(agent.tools) == 3  # fs_grep, fs_list_files, fs_read_file
