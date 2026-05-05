"""Specialist Agent factory — replaces BaseSpecialistAgent under the SDK."""
from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema, ModelSettings

from models.types import DomainSkill, SpecialistOutput
from skills.loader import load_skill as _load_skill
from tools.data_tools import (
    aggregate_column,
    get_table_schema,
    list_available_tables,
    query_table,
    summarize_by_group,
    summarize_trend,
)

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
        if "concept_glossary" in pillar and pillar["concept_glossary"]:
            parts.append(str(pillar["concept_glossary"]).strip())
        if "focus" in pillar:
            parts.append(f"Pillar focus: {pillar['focus']}")
        if "overlay" in pillar:
            parts.append(f"Pillar overlay: {pillar['overlay']}")
        if "cut_off_date" in pillar:
            cutoff = pillar["cut_off_date"]
            parts.append(
                f"DATA CUT-OFF DATE: {cutoff}.\n"
                f"CRITICAL — Interpret ALL time-window language ('recent', 'current', "
                f"'last N months', 'this year') relative to the cut-off, NEVER relative "
                f"to today's calendar date. 'Recent N months' = the N months ending on "
                f"{cutoff}. No data exists beyond {cutoff}.\n"
                f"For 'ramp-up' / 'ramp-up period', see the concept-glossary "
                f"definition — it is a DATA-DERIVED rising-then-stabilizing phase, "
                f"not a fixed-length window. Identify it from the relevant time "
                f"series before answering."
            )
    return "\n\n".join(parts)


def build_specialist_agent(skill: DomainSkill, pillar: dict, model) -> Agent:
    return Agent(
        name=skill.name,
        instructions=_compose_instructions(skill, pillar),
        tools=[list_available_tables, get_table_schema, query_table,
               aggregate_column, summarize_trend, summarize_by_group],
        output_type=AgentOutputSchema(SpecialistOutput, strict_json_schema=False),
        model=model,
        # Force the specialist to actually query the data on each invocation.
        # Without this, models sometimes hallucinate failures like "I was
        # unable to access the schemas" instead of calling get_table_schema /
        # query_table. ``reset_tool_choice=True`` (Agent default) auto-flips
        # this back to "auto" after the first tool call so the agent can
        # synthesize the SpecialistOutput.
        model_settings=ModelSettings(tool_choice="required"),
    )
