"""Specialist Agent factory — replaces BaseSpecialistAgent under the SDK."""
from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema

from models.types import DomainSkill, SpecialistOutput
from skills.loader import load_skill as _load_skill
from tools.data_tools import get_table_schema, list_available_tables, query_table

_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"
_BASE_INSTRUCTIONS = _load_skill(_WORKFLOW_DIR / "data_query.md").body


def _compose_instructions(skill: DomainSkill, pillar: dict) -> str:
    parts = [_BASE_INSTRUCTIONS,
             f"Domain: {skill.name}",
             f"Expertise: {skill.system_prompt}"]
    if skill.data_hints:
        parts.append(f"Data hints: {', '.join(skill.data_hints)}")
    if skill.interpretation_guide:
        parts.append(f"Interpretation guide: {skill.interpretation_guide}")
    if skill.risk_signals:
        parts.append(f"Risk signals: {', '.join(skill.risk_signals)}")
    if pillar:
        if "focus" in pillar:
            parts.append(f"Pillar focus: {pillar['focus']}")
        if "overlay" in pillar:
            parts.append(f"Pillar overlay: {pillar['overlay']}")
        if "cut_off_date" in pillar:
            cutoff = pillar["cut_off_date"]
            parts.append(
                f"DATA CUT-OFF DATE: {cutoff}\n"
                f"CRITICAL — Interpret ALL time-window language ('recent', 'current', "
                f"'last 3 months', 'this year') relative to this cut-off, NEVER relative "
                f"to today's calendar date.\n"
                f"Example: 'recent 3 months' means the three months ending on "
                f"{cutoff} (i.e., roughly the 3 months immediately preceding it), "
                f"NOT the 3 months before today. No data exists beyond {cutoff}."
            )
    return "\n\n".join(parts)


def build_specialist_agent(skill: DomainSkill, pillar: dict, model) -> Agent:
    return Agent(
        name=skill.name,
        instructions=_compose_instructions(skill, pillar),
        tools=[list_available_tables, get_table_schema, query_table],
        output_type=AgentOutputSchema(SpecialistOutput, strict_json_schema=False),
        model=model,
    )
