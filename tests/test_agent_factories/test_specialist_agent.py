import re

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
    pillar = {"focus": "credit"}
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
    # No configured cut-off date is injected any more — one pillar-wide
    # constant cannot be right for every case or every table, and stated as an
    # authority it got quoted as an observation. What survives is the guard,
    # which carries no date; the real cut-off is derived per case and arrives
    # with the round-1 inventory (§ DATA COVERAGE).
    assert "today's calendar date is NOT in this case's data" in prompt
    assert "CASE CUT-OFF in § DATA COVERAGE" in prompt
    # Scoped to § PILLAR CONTEXT — the skill bodies legitimately carry example
    # dates in filter snippets; what must stay dateless is the injected block.
    pillar_block = prompt.split("§ PILLAR CONTEXT", 1)[1].split("§ WORKFLOW", 1)[0]
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", pillar_block), \
        "a hard-coded cut-off date reappeared in the pillar context block"
    # 14 data tools + make_chart + get_chart_guidance + kb_list_topics + kb_lookup
    assert len(agent.tools) == 18
    assert {t.name for t in agent.tools} == {
        "list_available_tables", "get_table_schema", "search_columns",
        "query_table", "batch_query_table", "join_table", "sequence_join",
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


# ── the case's column inventory reaches the prompt that decides turn 1 ───────


def _real_data_layer():
    from datalayer.catalog import DataCatalog
    from datalayer.gateway import LocalDataGateway
    import tools.data_tools as data_tools

    gw = LocalDataGateway.from_case_folders("data_tables/real")
    gw.set_case("366132845011")
    data_tools.init_tools(gw, DataCatalog(profile_dir="config/data_profiles"))


def _modeling_agent():
    skill = DomainSkill(
        name="modeling", system_prompt="You analyze model scores.",
        data_hints=["model_scores", "score_drivers"],
    )
    return build_specialist_agent(skill, {}, model=None)


def test_round_one_prompt_carries_the_case_inventory():
    """Round 1 runs under tool_choice="required" — it MUST emit a tool call. What
    the specialist knows HERE decides whether that call is aimed or a blind
    probe, which is what exhausted `modeling`'s turn budget.

    Asserted against the RENDERED inventory, not its header: `data_query.md` now
    references the section by name, so a header check would pass even if nothing
    were injected.
    """
    from unittest.mock import MagicMock
    import tools.data_tools as data_tools

    _real_data_layer()
    agent = _modeling_agent()
    prompt = agent.instructions(MagicMock(), agent)

    inventory = data_tools.build_column_inventory(["model_scores", "score_drivers"])
    assert inventory, "fixture produced no inventory — the test proves nothing"
    assert inventory in prompt
    assert "modelling_data" in prompt              # the REAL table name
    assert "=INTOOP" in prompt                     # alias spellings, for recognition


def test_synthesis_rounds_do_not_re_pay_for_the_inventory():
    """By round 2 the specialist holds the DATA; re-sending the catalog would
    charge for it on every round."""
    from unittest.mock import MagicMock
    import tools.data_tools as data_tools

    _real_data_layer()
    agent = _modeling_agent()
    ctx = MagicMock()
    first = agent.instructions(ctx, agent)
    second = agent.instructions(ctx, agent)

    inventory = data_tools.build_column_inventory(["model_scores", "score_drivers"])
    assert inventory in first
    assert inventory not in second


def test_inventory_is_scoped_to_the_specialists_own_tables():
    from unittest.mock import MagicMock

    _real_data_layer()
    skill = DomainSkill(name="spend_payments", system_prompt="x",
                        data_hints=["spends"])
    agent = build_specialist_agent(skill, {}, model=None)
    prompt = agent.instructions(MagicMock(), agent)

    assert "spends_data" in prompt
    assert "modelling_data" not in prompt


def test_prompt_still_builds_without_a_data_layer():
    """Unit tests and any pre-case construction path must not blow up."""
    from unittest.mock import MagicMock
    import tools.data_tools as data_tools

    data_tools.init_tools(None, None)
    agent = _modeling_agent()
    prompt = agent.instructions(MagicMock(), agent)
    assert "You analyze model scores." in prompt
    # The rendered block always opens with this; the skill's reference to the
    # section by name must not be mistaken for the block itself.
    assert "Every column below is PRESENT" not in prompt
