"""General Specialist — cross-domain reviewer with Compare skill."""

from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema
from models.types import ReviewReport
from skills.loader import load_skill as _load_skill
from tools.data_tools import (
    aggregate_column,
    batch_aggregate,
    get_table_schema,
    list_available_tables,
)
from tools.data_viz_tools import build_make_chart_tool


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


COMPARE_SYSTEM_PROMPT = _load_skill(_WORKFLOW_DIR / "comparison.md").body


def build_general_specialist(model) -> Agent:
    # `general_specialist` gets:
    #   • `make_chart` — to render cross-domain comparison charts from
    #     numbers the specialists already surfaced.
    #   • Verification-only data tools — `list_available_tables`,
    #     `get_table_schema`, `aggregate_column`, `batch_aggregate`. These
    #     let it CHECK specialist claims (especially date / time anchors) by re-running
    #     the same aggregate, NOT to introduce new factual claims. See
    #     comparison.md's "Time/date consistency check" section for the
    #     narrow allowed-use pattern.
    #
    # Pointedly excluded: `query_table` (raw row dumps invite scope creep),
    # `summarize_trend` / `summarize_by_group` (those are specialist-level
    # analyses, not verification probes).
    make_chart = build_make_chart_tool("general_specialist")
    return Agent(
        name="general_specialist",
        instructions=COMPARE_SYSTEM_PROMPT,
        tools=[
            list_available_tables,
            get_table_schema,
            aggregate_column,
            batch_aggregate,
            make_chart,
        ],
        output_type=AgentOutputSchema(ReviewReport, strict_json_schema=False),
        model=model,
    )
