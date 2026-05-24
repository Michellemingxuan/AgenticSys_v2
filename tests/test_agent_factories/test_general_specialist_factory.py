from agents import Agent
from agent_factories.general_specialist import build_general_specialist
from models.types import ReviewReport


def test_build_general_specialist_returns_agent():
    agent = build_general_specialist(model=None)
    assert isinstance(agent, Agent)
    assert agent.name == "general_specialist"
    assert agent.output_type.output_type is ReviewReport
    # general_specialist gets six tools:
    #   • make_chart for cross-domain comparison charts.
    #   • get_chart_guidance — lazy-loaded chart-construction rulebook
    #     (returns data_viz.md). Called only when about to plot, so the
    #     ~7.9 KB of guidance doesn't sit in the default prompt.
    #   • list_available_tables / get_table_schema / aggregate_column /
    #     batch_aggregate — verification-only data tools that let it
    #     CHECK date/time anchors when specialists disagree on event
    #     timing (see comparison.md's "Time/date consistency check").
    #     Pointedly excludes query_table / summarize_trend /
    #     summarize_by_group — those are specialist-level analyses, not
    #     verification probes.
    tool_names = {t.name for t in agent.tools}
    assert tool_names == {
        "list_available_tables", "get_table_schema",
        "aggregate_column", "batch_aggregate", "make_chart",
        "get_chart_guidance",
    }
    assert "compare" in agent.instructions.lower() or "contradiction" in agent.instructions.lower()
    # data_viz.md is NOT in the default prompt anymore — its content is
    # returned by the get_chart_guidance tool on demand.
    assert "trend_dual" not in agent.instructions
    assert "threshold_<y_field>" not in agent.instructions
