"""Orchestrator Agent factory — A1 maximal: specialists + report + general as tools."""
from __future__ import annotations

from pathlib import Path

from agents import Agent

from case_agents.redacting_tool import redacting_tool
from models.types import FinalAnswer
from skills.loader import load_skill as _load_skill

_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


def _compose_orchestrator_instructions() -> str:
    parts = [
        _load_skill(_WORKFLOW_DIR / "team_construction.md").body,
        _load_skill(_WORKFLOW_DIR / "data_catalog.md").body,
        _load_skill(_WORKFLOW_DIR / "synthesis.md").body,
        _load_skill(_WORKFLOW_DIR / "balancing.md").body,
        (
            "PARALLEL EXECUTION: When multiple specialists are needed, emit ALL "
            "tool calls in a single response so they execute in parallel. Do not "
            "serialize specialist calls."
        ),
    ]
    return "\n\n---\n\n".join(parts)


def _describe_specialist(agent: Agent) -> str:
    return f"Domain specialist '{agent.name}' — call with a focused sub-question."


def build_orchestrator_agent(
    specialists: list[Agent],
    report_agent: Agent,
    general_specialist: Agent,
    model,
) -> Agent:
    tools = [
        redacting_tool(s, name=s.name, description=_describe_specialist(s))
        for s in specialists
    ]
    tools.append(redacting_tool(
        report_agent,
        name="report_agent",
        description="Look up prior curated reports for this case.",
    ))
    tools.append(redacting_tool(
        general_specialist,
        name="general_specialist",
        description="Compare specialist outputs and surface contradictions.",
    ))

    return Agent(
        name="orchestrator",
        instructions=_compose_orchestrator_instructions(),
        tools=tools,
        output_type=FinalAnswer,
        model=model,
    )
