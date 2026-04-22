"""Orchestrator — synthesis of specialist outputs into final answer."""

from __future__ import annotations

import json

from agents.session_registry import SessionRegistry
from config.report_loader import get_synthesis_prompt
from gateway.firewall_stack import FirewallStack
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


PLAN_TEAM_PROMPT = (
    "You are the orchestrator. Given a reviewer's ROOT question and a "
    "description of each available specialist (data tables and columns), "
    "produce a team plan: pick the specialists whose data can answer the "
    "root question, AND rewrite the root question into a focused sub-question "
    "for each.\n\n"
    "GOVERNING PRINCIPLE — sub-questions must be IN SERVICE of the root:\n"
    "- Every sub-question MUST be a piece of evidence whose answer directly "
    "contributes to answering the root question. If an answer to a sub-question "
    "would NOT change or support the answer to the root, the sub-question does "
    "not belong in the plan. Drop it.\n"
    "- Do NOT add sub-questions that merely expand scope, explore adjacent "
    "topics, or satisfy curiosity. No 'while we're at it' questions.\n"
    "- Before including a sub-question, silently ask yourself: \"If the "
    "specialist answers this, does it help the reviewer answer the root?\" "
    "If the honest answer is 'maybe' or 'only indirectly', drop it.\n\n"
    "OTHER GUIDELINES:\n"
    "- Select specialists whose DATA contains fields directly relevant to the "
    "root question. Prefer 1-3 specialists over a broad sweep.\n"
    "- Prefer warm specialists (already active in session) when relevant.\n"
    "- For broad questions (e.g. 'full report'), select all specialists.\n"
    "- Each sub-question must focus ONLY on the aspect that specialist's data "
    "can address. Do not ask a specialist about data they don't have.\n"
    "- If the question is atomic or can be answered by one specialist, the "
    "sub-question may equal the root question verbatim.\n"
    "- Sub-questions should be short (one sentence), grounded in the "
    "specialist's data vocabulary, and phrased so the answer slots directly "
    "into the root-question synthesis.\n\n"
    "Return a JSON object: "
    "{\"plan\": [{\"specialist\": \"<domain>\", \"sub_question\": \"<...>\"}, ...]}\n"
    "Always return at least one plan entry."
)


class Orchestrator:
    """Coordinates team planning, synthesis, and final answer assembly."""

    def __init__(
        self,
        firewall: FirewallStack,
        logger: EventLogger,
        registry: SessionRegistry,
        pillar: str,
        pillar_config: dict | None = None,
        catalog=None,
    ):
        self.firewall = firewall
        self.logger = logger
        self.registry = registry
        self.pillar = pillar
        self.pillar_config = pillar_config or {}
        self.catalog = catalog

    # ──────────────────────────────────────────────────────────────
    # Team planning — selects specialists AND decomposes the question
    # into per-specialist sub-questions in a single LLM call.
    # ──────────────────────────────────────────────────────────────

    def plan_team(
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

        spec_descriptions = self._build_specialist_descriptions(available_specialists)

        active_info = ""
        if active_specialists:
            active_info = "\nCurrently active (warm, have prior context):\n"
            for a in active_specialists:
                active_info += f"  - {a['domain']} ({a['questions_answered']} questions answered)\n"

        user_message = (
            f"Question: {question}\n"
            f"Pillar: {self.pillar}\n\n"
            f"Available specialists and their data:\n{spec_descriptions}\n"
            f"{active_info}\n"
            "Produce the team plan."
        )

        result = self.firewall.call(
            system_prompt=PLAN_TEAM_PROMPT,
            user_message=user_message,
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("plan_team_fallback", {"reason": "blocked"})
            return [TeamAssignment(specialist=s, sub_question=question)
                    for s in available_specialists]

        plan = self._parse_plan(result.data, available_specialists, question)
        self.logger.log("plan_team_done",
                        {"plan": [p.model_dump() for p in plan]})
        return plan

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

    def synthesize(
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
        result = self.firewall.call(
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
