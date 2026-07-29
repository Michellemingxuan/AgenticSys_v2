from agents import Agent
from agent_factories.specialist_agent import build_specialist_agent
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
    # Instructions is now a dynamic callable
    assert callable(agent.instructions)
    from unittest.mock import MagicMock
    mock_ctx = MagicMock()
    prompt = agent.instructions(mock_ctx, agent)
    assert "You analyze credit risk." in prompt
    assert "2025-12-01" in prompt
    # 13 data tools + make_chart + get_chart_guidance + kb_list_topics + kb_lookup
    assert len(agent.tools) == 17
    assert {t.name for t in agent.tools} == {
        "list_available_tables", "get_table_schema", "search_columns",
        "query_table", "batch_query_table", "join_table",
        "transaction_detail", "score_driver_values",
        "aggregate_column", "batch_aggregate",
        "summarize_trend", "batch_summarize_trend", "summarize_by_group",
        "make_chart", "get_chart_guidance",
        "kb_list_topics", "kb_lookup",
    }
    assert agent.model_settings.parallel_tool_calls is True
    # data_viz.md is NOT composed inline anymore — its content lives
    # behind the `get_chart_guidance` tool, lazy-loaded.
    # Charting guidance is now inline (specialist produces charts directly)
    assert "make_chart" in prompt
