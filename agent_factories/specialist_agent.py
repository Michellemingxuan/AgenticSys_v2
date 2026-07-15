"""Specialist Agent factory — replaces BaseSpecialistAgent under the SDK."""
from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema, ModelSettings

from models.types import DomainSkill, SpecialistOutput
from skills.loader import load_skill as _load_skill
from tools.data_tools import (
    aggregate_column,
    batch_aggregate,
    batch_query_table,
    batch_summarize_trend,
    get_table_schema,
    join_table,
    list_available_tables,
    query_table,
    summarize_by_group,
    summarize_trend,
    transaction_detail,
)
from tools.data_viz_tools import build_make_chart_tool, get_chart_guidance
from tools.kb_tools import kb_list_topics, kb_lookup

_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"
_BASE_INSTRUCTIONS = _load_skill(_WORKFLOW_DIR / "data_query.md").body

# NOTE: data_viz.md is NOT composed here anymore. Its ~7.9 KB of chart-
# construction guidance is loaded on-demand via the `get_chart_guidance`
# tool — specialists call it only when they've decided to plot, which
# keeps the default system prompt ~8 KB lighter for the majority of
# questions that never produce a chart.
# Shared chart-construction rules — read by every specialist that has
# `make_chart` in its tool list. Composed AFTER the data_query body so
# the data_query "Charting" pointer is followed by the actual rules.
# Same skill is composed into general_specialist; keeps chart rules
# from drifting between callers.


def _compose_instructions(skill: DomainSkill, pillar: dict) -> str:
    # Domain identity block — compact, always relevant
    domain_lines = [f"Domain: {skill.name}"]
    if skill.data_hints:
        domain_lines.append(f"Tables: {', '.join(skill.data_hints)}")
    if skill.interpretation_guide:
        domain_lines.append(f"Guide: {skill.interpretation_guide}")
    if skill.risk_signals:
        domain_lines.append(f"Risk signals: {'; '.join(skill.risk_signals)}")
    domain_block = "§ DOMAIN IDENTITY\n\n" + "\n".join(domain_lines)

    # Domain expertise (the full domain skill body)
    expertise_block = f"§ DOMAIN EXPERTISE\n\n{skill.system_prompt}"

    # Pillar context
    pillar_parts = []
    if pillar:
        if "cut_off_date" in pillar:
            cutoff = pillar["cut_off_date"]
            pillar_parts.append(
                f"DATA CUT-OFF DATE: {cutoff}. "
                f"Interpret ALL time-window language relative to this date, "
                f"NEVER today's calendar."
            )
        if "concept_glossary" in pillar and pillar["concept_glossary"]:
            pillar_parts.append(str(pillar["concept_glossary"]).strip())
        if "focus" in pillar:
            pillar_parts.append(f"Pillar focus: {pillar['focus']}")
        if "overlay" in pillar:
            pillar_parts.append(f"Pillar overlay: {pillar['overlay']}")

    sep = "\n\n────────────────────────────────────────\n\n"
    sections = [domain_block, expertise_block]
    if pillar_parts:
        sections.append("§ PILLAR CONTEXT\n\n" + "\n\n".join(pillar_parts))
    sections.append("§ WORKFLOW\n\n" + _BASE_INSTRUCTIONS)
    return sep.join(sections)


def build_specialist_agent(skill: DomainSkill, pillar: dict, model) -> Agent:
    # Per-specialist make_chart binding so the chart-creation tool knows
    # which KB list to append into when invoked. Charts created via this
    # tool are stored on the same KB the auto-distiller writes to and are
    # surfaced via the same `_collect_turn_charts` path in server.py.
    make_chart = build_make_chart_tool(skill.name)

    full_prompt = _compose_instructions(skill, pillar)
    # Build a lean synthesis prompt: drop § PLAN, § QUERY, § CROSS-DOMAIN
    # sections (only needed for tool-calling rounds). The SDK calls
    # get_system_prompt per-round, so R2 (synthesis) gets the shorter
    # prompt. Detection: tool_choice flips from "required" to "auto"
    # after the first tool call, which means R2+ has tools available but
    # the model has already queried — it just needs to emit output.
    sep = "\n\n────────────────────────────────────────\n\n"
    sections = full_prompt.split(sep)
    synthesis_prompt = sep.join(
        s for s in sections
        if not any(s.strip().startswith(tag) for tag in (
            "# § DATA QUERY", "# § CROSS-DOMAIN",
        ))
    )

    _call_count: dict[str, int] = {}

    def _dynamic_instructions(ctx, agent):
        key = id(ctx)
        count = _call_count.get(key, 0) + 1
        _call_count[key] = count
        if count > 1:
            return synthesis_prompt
        return full_prompt

    return Agent(
        name=skill.name,
        instructions=_dynamic_instructions,
        tools=[list_available_tables, get_table_schema, query_table,
               batch_query_table, join_table, transaction_detail,
               aggregate_column, batch_aggregate,
               summarize_trend, batch_summarize_trend, summarize_by_group,
               make_chart, get_chart_guidance,
               kb_list_topics, kb_lookup],
        output_type=AgentOutputSchema(SpecialistOutput, strict_json_schema=False),
        model=model,
        # Force the specialist to actually query the data on each invocation.
        # Without this, models sometimes hallucinate failures like "I was
        # unable to access the schemas" instead of calling get_table_schema /
        # query_table. ``reset_tool_choice=True`` (Agent default) auto-flips
        # this back to "auto" after the first tool call so the agent can
        # synthesize the SpecialistOutput.
        model_settings=ModelSettings(
            tool_choice="required",
            parallel_tool_calls=True,
            # max_tokens is a CEILING, not a target — a short SpecialistOutput
            # (~100-200 tok) stops at its natural end token regardless, so a
            # higher ceiling adds NO latency to normal rounds. It only bounds
            # the worst case. The binding round is the TOOL-CALL round: on
            # safechain the whole call is emitted as escaped TEXT
            # (`{"tool_call":{...,"specs_json":"[{...},{...}]"}}`), and a
            # 500-cap truncated a multi-trend `batch_summarize_trend` mid-
            # argument → `specs_json="["` → tool error → the specialist
            # fabricated the peaks. 3000 comfortably covers a 6-spec batch call
            # (verbose escaped text on safechain) plus any preamble, without
            # slowing the short synthesis round (ceiling, not target). Confirmed
            # in the private env: raising to 3000 cleared the truncated-specs
            # fabrication.
            max_tokens=3000,
        ),
    )
