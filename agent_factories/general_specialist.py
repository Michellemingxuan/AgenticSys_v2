"""General Specialist — cross-domain reviewer with Compare skill."""

from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema
from models.types import ReviewReport
from skills.loader import load_skill as _load_skill
from tools.data_viz_tools import build_make_chart_tool


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


COMPARE_SYSTEM_PROMPT = _load_skill(_WORKFLOW_DIR / "comparison.md").body


def build_general_specialist(model) -> Agent:
    # `make_chart` is the only tool general_specialist gets — it does NOT
    # query data (that's the domain specialists' job; comparison.md
    # forbids introducing new factual claims). What it CAN do is render a
    # cross-domain comparison chart from numbers the specialists already
    # surfaced — e.g. overlay delinquency indices from `modeling` with
    # returned-payment counts from `spend_payments` on the same axis.
    # Factory-bound so the chart KP lands under "general_specialist" and
    # the trace panel attaches it to the General Specialist Review block.
    make_chart = build_make_chart_tool("general_specialist")
    return Agent(
        name="general_specialist",
        instructions=COMPARE_SYSTEM_PROMPT,
        tools=[make_chart],
        output_type=AgentOutputSchema(ReviewReport, strict_json_schema=False),
        model=model,
    )
