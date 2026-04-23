"""Orchestrator — synthesis of specialist outputs into final answer."""

from __future__ import annotations

import json

from agents.session_registry import SessionRegistry
from config.report_loader import get_synthesis_prompt
from gateway.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import (
    BlockedStep,
    Conflict,
    DataGap,
    FinalOutput,
    Resolution,
    ReviewReport,
    SpecialistOutput,
    TeamAssignment,
)
from skills.domain.loader import load_domain_skill


SYNTHESIZE_PROMPT = (
    "You are the orchestrator synthesizer. Merge the following specialist "
    "outputs into a unified answer. Use resolved contradictions over raw "
    "findings when available. Evaluate absence of data as a potential signal "
    "(absence-as-signal). Never silently omit blocked or incomplete analyses — "
    "flag them explicitly.\n\n"
    "Output JSON with keys:\n"
    "- answer: the merged answer string\n"
    "- data_gap_assessments: list of objects with keys "
    "(specialist, missing_data, absence_interpretation, is_signal)\n"
)


SELECT_TEAM_PROMPT = (
    "You are the orchestrator's TEAM SELECTION step. Given a reviewer's "
    "root question and a description of each available specialist (data "
    "tables and columns), pick the specialists whose data can directly "
    "contribute to answering the root.\n\n"
    "RULES:\n"
    "- Select a specialist only if its DATA contains fields that the root "
    "question depends on. Prefer 1-3 specialists over a broad sweep.\n"
    "- Do NOT pick a specialist for 'additional context' or 'completeness'. "
    "Every pick must carry its weight in the final answer.\n"
    "- Prefer warm specialists (already active in session) when they are "
    "relevant — but do not pick a warm specialist whose data is unrelated.\n"
    "- For broad questions (e.g. 'full report'), select all specialists.\n"
    "- Return at least one specialist.\n\n"
    "Return a JSON object: {\"specialists\": [\"<domain1>\", \"<domain2>\"]}"
)


SPLIT_SUBQUESTIONS_PROMPT = (
    "You are the orchestrator's SUB-QUESTION DECOMPOSITION step. The team "
    "has already been selected. For each selected specialist, rewrite the "
    "root question into a focused sub-question.\n\n"
    "GOVERNING PRINCIPLE — sub-questions must be IN SERVICE of the root:\n"
    "- Every sub-question MUST be a piece of evidence whose answer directly "
    "contributes to answering the root question. If an answer to a sub-question "
    "would NOT change or support the answer to the root, it does not belong in "
    "the plan. Do not emit it.\n"
    "- Do NOT add sub-questions that merely expand scope, explore adjacent "
    "topics, or satisfy curiosity. No 'while we're at it' questions.\n"
    "- Before emitting each sub-question, silently ask yourself: \"If the "
    "specialist answers this, does it help the reviewer answer the root?\" "
    "If 'maybe' or 'only indirectly', skip or rewrite until tight.\n\n"
    "PHRASING RULES:\n"
    "- One sentence per sub-question.\n"
    "- Grounded in the specialist's data vocabulary (use column/table names "
    "from its data description where relevant).\n"
    "- Focused ONLY on the aspect that specialist's data can address — do not "
    "ask a specialist about data it doesn't have.\n"
    "- Orthogonal across specialists — two specialists must not be asked the "
    "same thing. Each sub-question gives the synthesizer a distinct piece.\n"
    "- Phrased so the answer slots directly into the root-question synthesis.\n"
    "- If only one specialist was selected, its sub-question may equal the "
    "root question verbatim.\n\n"
    "Return a JSON object: "
    "{\"plan\": [{\"specialist\": \"<domain>\", \"sub_question\": \"<...>\"}, ...]}\n"
    "Produce exactly one entry per selected specialist, in the same order."
)


class Orchestrator:
    """Coordinates team planning, synthesis, and final answer assembly."""

    def __init__(
        self,
        llm: FirewalledModel,
        logger: EventLogger,
        registry: SessionRegistry,
        pillar: str,
        pillar_config: dict | None = None,
        catalog=None,
    ):
        self.llm = llm
        self.logger = logger
        self.registry = registry
        self.pillar = pillar
        self.pillar_config = pillar_config or {}
        self.catalog = catalog

    # ──────────────────────────────────────────────────────────────
    # Team planning — two sequential LLM calls:
    #   1. _select_team()         → which specialists?
    #   2. _split_sub_questions() → what does each answer?
    # Kept as separate steps so the selection prompt stays focused on
    # data relevance and the decomposition prompt stays focused on
    # answer-quality + orthogonality. Report mode and single-specialist
    # cases short-circuit to avoid unnecessary LLM calls.
    # ──────────────────────────────────────────────────────────────

    async def plan_team(
        self,
        question: str,
        available_specialists: list[str],
        active_specialists: list[dict] | None = None,
        mode: str = "chat",
    ) -> list[TeamAssignment]:
        active_specialists = active_specialists or []
        self.logger.log(
            "plan_team_start",
            {"question": question, "pillar": self.pillar, "mode": mode,
             "available": available_specialists},
        )

        # Report mode → consult all specialists, each gets the root question
        # verbatim; a full report doesn't benefit from splitting.
        if mode == "report":
            plan = [TeamAssignment(specialist=s, sub_question=question)
                    for s in available_specialists]
            self.logger.log("plan_team_done",
                            {"plan": [p.model_dump() for p in plan],
                             "reason": "report mode — all specialists, root question"})
            return plan

        # Step 1: team selection.
        selected = await self._select_team(question, available_specialists, active_specialists)

        # Single-specialist shortcut: no decomposition needed.
        if len(selected) <= 1:
            plan = [TeamAssignment(specialist=s, sub_question=question)
                    for s in selected]
            self.logger.log("plan_team_done",
                            {"plan": [p.model_dump() for p in plan],
                             "reason": "single specialist — sub-question equals root"})
            return plan

        # Step 2: sub-question decomposition for the selected team.
        plan = await self._split_sub_questions(question, selected)
        self.logger.log("plan_team_done",
                        {"plan": [p.model_dump() for p in plan]})
        return plan

    async def _select_team(
        self,
        question: str,
        available_specialists: list[str],
        active_specialists: list[dict],
    ) -> list[str]:
        """LLM call #1: pick specialists whose data serves the root question.

        Shows the FULL data catalog (every table + every column with
        descriptions) plus a specialist→tables roster. This is the only
        step that sees the complete catalog; step 2 narrows to per-
        specialist detail via ``_build_specialist_descriptions``.
        """
        catalog_view = ""
        if self.catalog is not None:
            catalog_view = self.catalog.to_prompt_context() + "\n"

        roster_lines = ["=== SPECIALIST ROSTER ==="]
        for domain in available_specialists:
            skill = load_domain_skill(domain)
            if skill is None:
                roster_lines.append(f"- {domain}: (no skill loaded)")
                continue
            tables = ", ".join(skill.data_hints) if skill.data_hints else "(no tables)"
            roster_lines.append(f"- {domain} — tables: {tables}")
        roster = "\n".join(roster_lines)

        active_info = ""
        if active_specialists:
            active_info = "\nCurrently active (warm, have prior context):\n"
            for a in active_specialists:
                active_info += f"  - {a['domain']} ({a['questions_answered']} questions answered)\n"

        user_message = (
            f"Root question: {question}\n"
            f"Pillar: {self.pillar}\n\n"
            f"{catalog_view}"
            f"{roster}\n"
            f"{active_info}\n"
            "Pick the specialists whose data directly contributes to the root."
        )

        result = await self.llm.ainvoke(
            system_prompt=SELECT_TEAM_PROMPT,
            user_message=user_message,
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("select_team_fallback",
                            {"reason": "blocked — defaulting to all"})
            return list(available_specialists)

        selected = self._parse_team_selection(result.data, available_specialists)
        self.logger.log("select_team_done", {"selected": selected})
        return selected

    async def _split_sub_questions(
        self,
        question: str,
        selected_specialists: list[str],
    ) -> list[TeamAssignment]:
        """LLM call #2: given the already-selected team, write per-specialist sub-questions."""
        spec_descriptions = self._build_specialist_descriptions(selected_specialists)

        user_message = (
            f"Root question: {question}\n"
            f"Pillar: {self.pillar}\n\n"
            f"Selected specialists (exactly these, in order):\n{spec_descriptions}\n\n"
            "Produce one sub-question per specialist, each in service of the root."
        )

        result = await self.llm.ainvoke(
            system_prompt=SPLIT_SUBQUESTIONS_PROMPT,
            user_message=user_message,
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("split_sub_questions_fallback",
                            {"reason": "blocked — using root for each"})
            return [TeamAssignment(specialist=s, sub_question=question)
                    for s in selected_specialists]

        return self._parse_plan(result.data, selected_specialists, question)

    def _build_specialist_descriptions(self, available: list[str]) -> str:
        lines: list[str] = []
        for domain in available:
            skill = load_domain_skill(domain)
            if skill is None:
                lines.append(f"- {domain}: (no skill loaded)")
                continue

            desc = f"- {domain}:"
            desc += f"\n    Focus: {skill.system_prompt[:150]}"
            desc += f"\n    Tables: {', '.join(skill.data_hints)}"

            if self.catalog:
                for table in skill.data_hints:
                    schema = self.catalog.get_schema(table)
                    if schema:
                        col_names = list(schema.keys())
                        if len(col_names) > 15:
                            desc += f"\n    Columns ({table}): {', '.join(col_names[:15])}... (+{len(col_names)-15} more)"
                        else:
                            desc += f"\n    Columns ({table}): {', '.join(col_names)}"

            desc += f"\n    Risk signals: {', '.join(skill.risk_signals[:3])}"
            lines.append(desc)

        return "\n".join(lines)

    def _parse_team_selection(
        self,
        data: dict,
        available: list[str],
    ) -> list[str]:
        """Parse the JSON returned by the SELECT_TEAM_PROMPT step."""
        raw = data.get("specialists", data.get("response", []))

        if isinstance(raw, str):
            try:
                parsed_outer = json.loads(raw)
                raw = parsed_outer.get("specialists", [])
            except (json.JSONDecodeError, AttributeError):
                return list(available)

        if not isinstance(raw, list):
            return list(available)

        validated: list[str] = []
        seen: set[str] = set()
        for name in raw:
            if isinstance(name, str) and name in available and name not in seen:
                validated.append(name)
                seen.add(name)

        if not validated:
            return list(available)
        return validated

    def _parse_plan(
        self,
        data: dict,
        available: list[str],
        root_question: str,
    ) -> list[TeamAssignment]:
        raw = data.get("plan", data.get("response", []))

        if isinstance(raw, str):
            try:
                parsed_outer = json.loads(raw)
                raw = parsed_outer.get("plan", [])
            except (json.JSONDecodeError, AttributeError):
                return [TeamAssignment(specialist=s, sub_question=root_question)
                        for s in available]

        if not isinstance(raw, list):
            return [TeamAssignment(specialist=s, sub_question=root_question)
                    for s in available]

        plan: list[TeamAssignment] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            specialist = entry.get("specialist") or entry.get("domain")
            sub_q = entry.get("sub_question") or entry.get("subquestion") or root_question
            if specialist in available and specialist not in seen:
                plan.append(TeamAssignment(specialist=specialist, sub_question=sub_q))
                seen.add(specialist)

        if not plan:
            return [TeamAssignment(specialist=s, sub_question=root_question)
                    for s in available]

        return plan

    async def synthesize(
        self,
        specialist_outputs: dict[str, SpecialistOutput],
        review_report: ReviewReport,
        question: str,
        mode: str,
        team_plan: list[TeamAssignment] | None = None,
    ) -> FinalOutput:
        self.logger.log(
            "orchestrator_synthesize",
            {
                "question": question,
                "mode": mode,
                "specialists": list(specialist_outputs.keys()),
            },
        )

        # Build synthesis context
        context_parts: list[str] = []

        # Specialist outputs
        for domain, output in specialist_outputs.items():
            context_parts.append(
                f"[{domain}]\n"
                f"Findings: {output.findings}\n"
                f"Evidence: {', '.join(output.evidence)}\n"
                f"Implications: {', '.join(output.implications)}\n"
                f"Data gaps: {', '.join(output.data_gaps)}\n"
            )

        # Resolved contradictions
        if review_report.resolved:
            context_parts.append("RESOLVED CONTRADICTIONS:")
            for r in review_report.resolved:
                context_parts.append(
                    f"  {r.pair}: {r.contradiction} -> {r.conclusion}"
                )

        # Open conflicts
        if review_report.open_conflicts:
            context_parts.append("OPEN CONFLICTS:")
            for c in review_report.open_conflicts:
                context_parts.append(
                    f"  {c.pair}: {c.contradiction} ({c.reason_unresolved})"
                )

        # Cross-domain insights
        if review_report.cross_domain_insights:
            context_parts.append(
                "CROSS-DOMAIN INSIGHTS:\n"
                + "\n".join(f"  - {ins}" for ins in review_report.cross_domain_insights)
            )

        synthesis_context = "\n\n".join(context_parts)
        user_message = (
            f"Question: {question}\n"
            f"Mode: {mode}\n\n"
            f"SYNTHESIZE the following:\n\n{synthesis_context}"
        )

        synthesis_prompt = get_synthesis_prompt(
            mode=mode,
            pillar_report_format=self.pillar_config.get("report_format", ""),
            pillar_synthesis_report=self.pillar_config.get("synthesis_report", ""),
        )
        result = await self.llm.ainvoke(
            system_prompt=synthesis_prompt,
            user_message=user_message,
        )

        if result.status == "blocked" or result.data is None:
            return FinalOutput(
                answer="Synthesis was blocked by content firewall. Please rephrase.",
                specialists_consulted=list(specialist_outputs.keys()),
                open_conflicts=review_report.open_conflicts,
            )

        data = result.data
        answer = data.get("answer", data.get("response", "No answer generated"))
        data_gap_summary = data.get("data_gap_summary", "")

        # Parse data gap assessments
        data_gaps: list[DataGap] = []
        for gap_data in data.get("data_gap_assessments", []):
            gap = DataGap(
                specialist=gap_data.get("specialist", ""),
                missing_data=gap_data.get("missing_data", ""),
                absence_interpretation=gap_data.get("absence_interpretation", ""),
                is_signal=gap_data.get("is_signal", False),
            )
            data_gaps.append(gap)
            self.logger.log(
                "data_gap_flagged",
                {
                    "specialist": gap.specialist,
                    "missing_data": gap.missing_data,
                    "is_signal": gap.is_signal,
                },
            )

        # Also collect data gaps from specialist outputs
        for domain, output in specialist_outputs.items():
            for gap_str in output.data_gaps:
                if not any(g.specialist == domain and g.missing_data == gap_str for g in data_gaps):
                    data_gaps.append(
                        DataGap(
                            specialist=domain,
                            missing_data=gap_str,
                            absence_interpretation="Not assessed",
                            is_signal=False,
                        )
                    )

        # Detect blocked steps
        blocked_steps: list[BlockedStep] = []
        for domain, output in specialist_outputs.items():
            if "blocked" in output.findings.lower() and "incomplete" in output.findings.lower():
                blocked_steps.append(
                    BlockedStep(
                        specialist=domain,
                        step=output.findings,
                        error="Analysis blocked by firewall",
                        attempts=1,
                    )
                )

        return FinalOutput(
            answer=answer,
            data_gap_summary=data_gap_summary,
            resolved_contradictions=review_report.resolved,
            open_conflicts=review_report.open_conflicts,
            cross_domain_insights=review_report.cross_domain_insights,
            data_requests_made=review_report.data_requests_made,
            data_gaps=data_gaps,
            blocked_steps=blocked_steps,
            specialists_consulted=list(specialist_outputs.keys()),
            sub_questions=team_plan or [],
        )
