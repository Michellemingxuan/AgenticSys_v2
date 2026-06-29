"""The report agent is wired with fs_grep alongside the existing fs tools."""
from agent_factories.report_agent import build_report_agent


def test_report_agent_exposes_fs_grep():
    agent = build_report_agent(model="gpt-4.1")
    names = {t.name for t in agent.tools}
    assert {"fs_grep", "fs_list_files", "fs_read_file"} <= names
