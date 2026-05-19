"""Orchestrator Agent factory — A1 maximal: specialists + report + general as tools."""
from __future__ import annotations

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
        # Lead with the HARD GATE. Tight enough that no downstream reminder
        # is needed; every load-bearing rule (count basis, both sides of
        # the conditional, Round 2.5 re-answer on `corrected_specialist`,
        # multi-correction collapse) is here.
        (
            "★ HARD GATE — `general_specialist` is MANDATORY on 2+ domain "
            "specialist turns, FORBIDDEN on 1-specialist turns ★\n"
            "\n"
            "Before FinalAnswer: count UNIQUE DOMAIN specialists called this "
            "turn (exclude `report_agent` and `general_specialist`).\n"
            "  • count = 1 → MUST NOT call `general_specialist` (adds 30-60s "
            "for zero content — one specialist can't disagree with itself). "
            "Finalize from the lone specialist + report_agent.\n"
            "  • count ≥ 2 → MUST call `general_specialist` after Round 1. "
            "Then scan its `resolved` entries: every entry with a non-null "
            "`corrected_specialist` triggers a Round 2.5 re-invocation of "
            "THAT specialist with the canonical value folded in. Multiple "
            "corrections for the same specialist → ONE combined re-call. "
            "THEN FinalAnswer using the post-correction outputs.\n"
            "\n"
            "Round protocol when count ≥ 2:\n"
            "  R1 (parallel): domain specialists + report_agent in one "
            "response. R2: general_specialist. R2.5 (conditional): "
            "re-invoke specialists named in `resolved.corrected_specialist`. "
            "R3: FinalAnswer.\n"
            "\n"
            "Round-2.5 sub-question shape: *\"Re-answer your earlier "
            "question. General specialist verified <corrected_value> for "
            "<what was checked> (canonical aggregate). Revise your "
            "`findings` / `evidence` / `implications` against this canonical "
            "value.\"*\n"
            "\n"
            "Emitting FinalAnswer without `general_specialist` on a 2+ turn, "
            "or skipping a required Round 2.5, is a protocol violation."
        ),
        _load_skill(_WORKFLOW_DIR / "team_construction.md").body,
        _load_skill(_WORKFLOW_DIR / "data_catalog.md").body,
        _load_skill(_WORKFLOW_DIR / "synthesis.md").body,
        (
            "TOOL-USE DISCIPLINE (unconditional): Before emitting a "
            "FinalAnswer you MUST have called BOTH (1) report_agent and "
            "(2) at least one domain specialist tool. No loopholes — "
            "report_agent text alone is never sufficient grounding, even "
            "with coverage='explicit'. If no specialist seems directly "
            "relevant, pick the closest one and let it return a data_gap. "
            "Every FinalAnswer claim must trace to a tool result this run "
            "produced; never answer from schema inference or general "
            "knowledge.\n\n"
            "PARALLEL EXECUTION: Emit report_agent + every domain "
            "specialist in a SINGLE response so they run in parallel."
        ),
    ]
    # Pillar-wide concept glossary (consumer/commercial, balance/spend, etc.)
    # — same content the specialists see, so orchestrator routing decisions
    # use the same canonical vocabulary as specialist filter construction.
    if pillar_config and pillar_config.get("concept_glossary"):
        parts.append(str(pillar_config["concept_glossary"]).strip())
    if specialists:
        parts.append(_render_team_roster(specialists, catalog=catalog))
    return "\n\n---\n\n".join(parts)


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
    general_specialist: Agent,
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
    ))
    tools.append(redacting_tool(
        general_specialist,
        name="general_specialist",
        description="Compare specialist outputs and surface contradictions.",
    ))

    return Agent(
        name="orchestrator",
        instructions=_compose_orchestrator_instructions(
            specialists=specialists, catalog=catalog, pillar_config=pillar_config
        ),
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
        # ``max_tokens=4096``: the orchestrator's FinalAnswer carries the
        # narrative answer + flags. Previously 8192 to avoid truncation,
        # but on simple 1-specialist questions ("how many successful
        # payments") the synthesis step was taking 20+s — the model was
        # using most of the budget. The synthesis skill targets 6-12
        # lines of markdown which fits comfortably in 4096 tokens; the
        # Pydantic schema-validation failure mode (a long FinalAnswer
        # JSON truncated mid-emit) is also caught by the orchestrator
        # retry loop in server.py, so this cap is safe.
        # 4096 keeps p95 synthesis time well under 10s on typical
        # 1-specialist turns while leaving headroom for multi-specialist
        # synthesis with `flags` / `cross_domain_insights` lists.
        #
        # ``parallel_tool_calls=True``: opts into OpenAI's parallel function
        # calling — the model may emit multiple ``function_call`` items in a
        # SINGLE assistant message, and the SDK runs them concurrently. The
        # ROUND 1 protocol above explicitly relies on this (report_agent +
        # all domain specialists on the same turn). Without the explicit
        # flag the default is model-defined and we observed gpt-4.1 sometimes
        # serializing calls one-per-turn — case 366132845011 spent 31s + 106s
        # on spend_payments → modeling in strict cascade rather than max(31,
        # 106) in parallel. tool_choice="required" doesn't block
        # parallelism; it requires AT LEAST one tool call, not exactly one.
        model_settings=ModelSettings(
            tool_choice="required",
            parallel_tool_calls=True,
            max_tokens=4096,
        ),
    )
