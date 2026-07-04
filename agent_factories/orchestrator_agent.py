"""Orchestrator Agent factory — specialists + report as tools.

Coherence review is enforced SERVER-SIDE (``server._run_review`` builds its own
``general_specialist``); the orchestrator no longer self-calls a reviewer tool.
"""
from __future__ import annotations

import os
from pathlib import Path

from agents import Agent, AgentOutputSchema, ModelSettings

from agent_factories.redacting_tool import redacting_tool
from models.types import FinalAnswer
from skills.domain.loader import load_domain_skill as _load_domain_skill
from skills.loader import load_skill as _load_skill

_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


def _render_team_roster(specialists: list[Agent], catalog=None) -> str:
    """Build a dynamic per-specialist roster, table-by-table, for the prompt.

    For each specialist agent, looks up its DomainSkill (description +
    data_hints) and, when a DataCatalog is supplied, the per-table
    descriptions for the tables that specialist owns. The result is a
    concrete, name-grounded routing reference that the orchestrator reads
    BEFORE deciding which specialist tool to call. Without this, the LLM
    sees only the tool docstrings — fine for unambiguous cases but
    fragile when reviewer phrasing doesn't match a tool's docstring.
    """
    lines: list[str] = ["=== TEAM ROSTER (auto-generated from skills + catalog) ==="]
    for s in specialists:
        skill = _load_domain_skill(s.name)
        domain_desc = (skill.description.strip() if skill else "(no skill loaded)")
        lines.append(f"\n• {s.name} — {domain_desc}")

        hints = list(skill.data_hints) if skill else []
        if not hints:
            lines.append("    owns no declared data tables.")
            continue

        for table in hints:
            tbl_desc = ""
            if catalog is not None:
                tbl_desc = (catalog.get_description(table) or "").strip()
            if tbl_desc:
                # First sentence is usually the most useful — keep it tight.
                first = tbl_desc.split(". ")[0].strip().rstrip(".")
                lines.append(f"    owns `{table}`: {first}.")
            else:
                lines.append(f"    owns `{table}`.")

        # Surface a couple of risk-signal phrases so the orchestrator
        # can also route by concern type, not just data table name.
        if skill and skill.risk_signals:
            top = skill.risk_signals[:3]
            lines.append(f"    flags risks like: {'; '.join(top)}.")
    lines.append(
        "\nROUTING RULE: pick the specialist whose `owns` table most directly "
        "carries the reviewer's question. Prefer 1–2 specialists; only widen "
        "to 3+ when the question explicitly spans multiple domains."
    )
    return "\n".join(lines)


def _compose_orchestrator_instructions(
    specialists: list[Agent] | None = None,
    catalog=None,
    pillar_config: dict | None = None,
) -> str:
    parts = [
        # ── § PROTOCOL (applies to all rounds) ──────────────────────
        (
            "§ PROTOCOL\n\n"
            "Cross-specialist coherence review is handled SERVER-SIDE after "
            "your dispatch round — you do NOT call any reviewer tool. Dispatch "
            "the specialists you need, then synthesize. If the server detects a "
            "cross-specialist contradiction it injects a `[REVIEW DIRECTIVE]` "
            "user turn and re-runs you; incorporate that directive when it is "
            "present in your input. Report-vs-specialist conflicts are resolved "
            "by YOU in synthesis.\n\n"
            "Round protocol:\n"
            "  1 specialist:  R1 → specialist + report_agent → R2 → FinalAnswer\n"
            "  2+ specialists: R1 → specialists + report_agent → R2 → FinalAnswer\n\n"
            "TOOL-USE DISCIPLINE: Before FinalAnswer, MUST have called "
            "BOTH (1) report_agent and (2) at least one domain specialist. "
            "You are the manager. Dispatch the team by judgment (see the "
            "team_construction Dispatch-shape guidance): prefer parallel for "
            "independent sub-questions; collapse a causal dependency into one "
            "cross-querying specialist when possible; sequence only when an "
            "anchor needs another specialist's deep analysis first. Emit "
            "independent specialist calls together so they run in parallel."
        ),
        # ── § SYNTHESIS (how to produce FinalAnswer — read every round) ─
        "§ SYNTHESIS\n\n" + _load_skill(_WORKFLOW_DIR / "synthesis.md").body,
        # ── § R1 REFERENCE (routing + catalog — skip when synthesizing) ─
        "§ R1 REFERENCE (team selection + data catalog — skip when synthesizing)\n\n"
        + _load_skill(_WORKFLOW_DIR / "team_construction.md").body
        + "\n\n---\n\n"
        + _load_skill(_WORKFLOW_DIR / "data_catalog.md").body,
    ]
    # Pillar-wide concept glossary (consumer/commercial, balance/spend, etc.)
    # — same content the specialists see, so orchestrator routing decisions
    # use the same canonical vocabulary as specialist filter construction.
    if pillar_config and pillar_config.get("concept_glossary"):
        parts.append(str(pillar_config["concept_glossary"]).strip())
    if specialists:
        parts.append(_render_team_roster(specialists, catalog=catalog))
    return "\n\n────────────────────────────────────────\n\n".join(parts)


def _describe_specialist(agent: Agent) -> str:
    """Build a routing-rich description from the specialist's domain skill.

    Without this enrichment, the orchestrator sees every specialist tool with
    the same generic blurb ("Domain specialist 'X' — call with a focused
    sub-question.") and routes blind — e.g. sending a "consumer cards"
    question to ``wcc`` (which owns customer-service logs, not cards). With
    the skill's ``description`` and ``data_hints`` in the tool description,
    the orchestrator can match question topics to the specialist whose data
    actually carries the answer.
    """
    skill = _load_domain_skill(agent.name)
    if skill is None:
        return f"Domain specialist '{agent.name}' — call with a focused sub-question."

    desc_short = (skill.description or "").strip()
    hints = ", ".join(skill.data_hints) if skill.data_hints else "(no data tables)"

    return (
        f"Domain specialist '{agent.name}'. {desc_short} "
        f"Owns data tables: {hints}. "
        f"Call with a focused sub-question scoped to this domain."
    )


def build_orchestrator_agent(
    specialists: list[Agent],
    report_agent: Agent,
    model,
    catalog=None,
    pillar_config: dict | None = None,
) -> Agent:
    tools = [
        redacting_tool(s, name=s.name, description=_describe_specialist(s))
        for s in specialists
    ]
    tools.append(redacting_tool(
        report_agent,
        name="report_agent",
        description="Look up prior curated reports for this case.",
        # Auxiliary lookup, on the R1 critical path: a shallow file read,
        # NOT analysis. Give it a tight budget so a stalled safechain round
        # fails fast and best-effort (orchestrator continues without the
        # report draft) instead of dragging the turn toward the 240s fence.
        timeout_s=float(os.environ.get("REPORT_AGENT_TIMEOUT_S", "100")),
        max_turns=int(os.environ.get("REPORT_AGENT_MAX_TURNS", "2")),
    ))

    full_prompt = _compose_orchestrator_instructions(
        specialists=specialists, catalog=catalog, pillar_config=pillar_config
    )
    # Build a lean synthesis-only prompt for R2+ (drops the 9687-char
    # R1 REFERENCE section). The SDK calls get_system_prompt per-round,
    # so we detect round via _domain_specialists_called on AppContext.
    sep = "\n\n────────────────────────────────────────\n\n"
    sections = full_prompt.split(sep)
    synthesis_prompt = sep.join(
        s for s in sections
        if not s.strip().startswith("§ R1 REFERENCE")
    )

    def _dynamic_instructions(ctx, agent):
        app_ctx = ctx.context if ctx else None
        called = getattr(app_ctx, "_domain_specialists_called", None)
        if isinstance(called, set) and len(called) > 0:
            return synthesis_prompt
        return full_prompt

    return Agent(
        name="orchestrator",
        instructions=_dynamic_instructions,
        tools=tools,
        # FinalAnswer has Optional fields (report_draft, team_draft, etc.)
        # which OpenAI's strict JSON schema rejects. Disable strict mode.
        output_type=AgentOutputSchema(FinalAnswer, strict_json_schema=False),
        model=model,
        # Force at least one tool call per Runner.run. Without this, gpt-4.1
        # (and other strict-instruction-following models) sometimes skip
        # specialists and emit a FinalAnswer directly from the system
        # prompt's text — which violates the TOOL-USE DISCIPLINE rule above
        # and produces ungrounded answers. ``reset_tool_choice=True`` is the
        # SDK default, so after the first tool call this auto-flips back to
        # ``"auto"`` and the agent can synthesize the FinalAnswer normally.
        #
        # ``max_tokens=2000``: FinalAnswer = answer (6-12 lines markdown,
        # ~200-600 tokens) + flags (0-3 bullets) + data_pull_request
        # (optional, ~50 tokens). Fast-path 1-specialist answers are
        # 30-50 tokens. 2000 covers the p99 multi-specialist synthesis
        # while cutting generation time on simple questions (~40% faster
        # than 4096 at 50 tok/s). Truncation is caught by the server
        # retry loop.
        #
        # ``parallel_tool_calls=True``: opts into OpenAI's parallel function
        # calling — the model may emit multiple ``function_call`` items in a
        # SINGLE assistant message, and the SDK runs them concurrently.
        model_settings=ModelSettings(
            tool_choice="required",
            parallel_tool_calls=True,
            max_tokens=2000,
        ),
    )
