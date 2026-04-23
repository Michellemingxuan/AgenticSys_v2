"""Orchestrator — synthesis of specialist outputs into final answer."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from agents.general_specialist import GeneralSpecialist
from agents.report_agent import ReportAgent
from agents.session_registry import SessionRegistry
from config.report_loader import get_synthesis_prompt
from gateway.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import (
    BlockedStep,
    Conflict,
    DataGap,
    FinalAnswer,
    ReportDraft,
    Resolution,
    ReviewReport,
    SpecialistOutput,
    TeamAssignment,
    TeamDraft,
)
from skills.domain.loader import list_domain_skills, load_domain_skill
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


def _extract_section(body: str, heading_prefix: str) -> str:
    """Extract a markdown section starting with the given heading prefix.

    Returns everything from the matched heading until the next top-level
    heading (or end of body). Used to split `team_construction.md`'s two
    step-blocks so each LLM call sees only its own instructions.
    """
    pattern = rf"(?ms)^({re.escape(heading_prefix)}[^\n]*\n\n.*?)(?=\n^# |\Z)"
    m = re.search(pattern, body)
    if m is None:
        return body
    section = m.group(1)
    # Drop the leading heading line + blank line that follows it.
    return re.sub(r"^#[^\n]*\n\n", "", section, count=1).rstrip()


_TEAM_CONSTRUCTION_BODY = _load_skill(_WORKFLOW_DIR / "team_construction.md").body

SYNTHESIZE_PROMPT = _load_skill(_WORKFLOW_DIR / "synthesis.md").body
SELECT_TEAM_PROMPT = _extract_section(_TEAM_CONSTRUCTION_BODY, "# Step 1")
SPLIT_SUBQUESTIONS_PROMPT = _extract_section(_TEAM_CONSTRUCTION_BODY, "# Step 2")
BALANCING_PROMPT = _load_skill(_WORKFLOW_DIR / "balancing.md").body
DATA_CATALOG_PROMPT = _load_skill(_WORKFLOW_DIR / "data_catalog.md").body


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
    ) -> list[TeamAssignment]:
        active_specialists = active_specialists or []
        self.logger.log(
            "plan_team_start",
            {"question": question, "pillar": self.pillar,
             "available": available_specialists},
        )

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
            system_prompt=SELECT_TEAM_PROMPT + "\n\n" + DATA_CATALOG_PROMPT,
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
    ) -> TeamDraft:
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
            system_prompt=synthesis_prompt + "\n\n" + DATA_CATALOG_PROMPT,
            user_message=user_message,
        )

        if result.status == "blocked" or result.data is None:
            return TeamDraft(
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

        return TeamDraft(
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

    # ──────────────────────────────────────────────────────────────
    # Phase 4 — parallel Reports + Team pipeline with Balancing merge
    # ──────────────────────────────────────────────────────────────

    async def run(
        self,
        question: str,
        case_folder: Path,
        report_agent: ReportAgent,
    ) -> FinalAnswer:
        """End-to-end per-question entry: dispatch Reports + Team in parallel,
        merge via the Balancing skill, return a FinalAnswer.

        The report_agent is injected so tests can stub it; production callers
        build one via `ReportAgent(llm, logger)` and hand it in once per session.
        """
        self.logger.log(
            "orchestrator_run_start",
            {"question": question, "case_folder": str(case_folder)},
        )

        # Per-stage wall-clock timeline — cheap forward-looking hook for the
        # chat UI to show progress / duration without a full LangGraph rewrite.
        # Each entry records ISO8601 timestamps + a perf-counter-derived
        # duration in ms. The two parallel branches overlap in time.
        timeline: list[dict] = []

        async def _timed(stage: str, coro):
            started = datetime.now(timezone.utc)
            t0 = time.perf_counter()
            try:
                return await coro
            finally:
                timeline.append({
                    "stage": stage,
                    "started_at": started.isoformat(),
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
                })

        report_draft, team_draft = await asyncio.gather(
            _timed("report_agent", report_agent.run(question, case_folder)),
            _timed("team_workflow", self._run_team_workflow(question)),
        )

        # Each parallel branch hands its draft back to the orchestrator.
        # Route both through the firewall bus for redact + logging + shape
        # round-trip, before the balancing step combines them.
        firewall = self.llm.firewall
        report_draft = await firewall.send(
            report_draft, from_agent="report_agent", to_agent="orchestrator"
        )
        team_draft = await firewall.send(
            team_draft, from_agent="team_workflow", to_agent="orchestrator"
        )

        self.logger.log(
            "orchestrator_run_branches_done",
            {
                "report_coverage": report_draft.coverage,
                "team_specialists": team_draft.specialists_consulted,
            },
        )

        final = await _timed("balance", self.balance(question, report_draft, team_draft))
        final = await firewall.send(
            final, from_agent="orchestrator", to_agent="chat_agent"
        )

        # Attach timeline AFTER firewall.send — ISO timestamps carry 6-digit
        # microseconds that the redact regex (`\d{6,}`) would otherwise mask
        # (e.g. "2026-04-24T10:00:03.***MASKED***+00:00"). Timestamps aren't
        # sensitive identifiers, so set them on the final returned object.
        final.timeline = timeline

        self.logger.log(
            "orchestrator_run_done",
            {
                "flag_count": len(final.flags),
                "answer_len": len(final.answer),
                "timeline_stages": [t["stage"] for t in timeline],
            },
        )
        return final

    async def _run_team_workflow(self, question: str) -> TeamDraft:
        """Team-side branch: plan → dispatch specialists in parallel → compare → synthesize.

        Mode is fixed to "chat" — the legacy "report" mode is deprecated
        (the `--mode report` CLI flag is dropped in this phase; the Report
        Agent consumes pre-staged reports rather than regenerating them).
        """
        mode = "chat"
        available = list_domain_skills()
        active = self.registry.list_active()

        plan = await self.plan_team(
            question=question,
            available_specialists=available,
            active_specialists=active,
        )

        # Fan out specialists concurrently. Registry is in-memory so `get_or_create`
        # is safe to call sequentially before the gather.
        async def _dispatch(assignment: TeamAssignment) -> tuple[str, SpecialistOutput] | None:
            skill = load_domain_skill(assignment.specialist)
            if skill is None:
                return None
            agent = self.registry.get_or_create(
                domain=assignment.specialist,
                pillar=self.pillar,
                domain_skill=skill,
                pillar_yaml=self.pillar_config,
                llm=self.llm,
                logger=self.logger,
            )
            output = await agent.run(
                assignment.sub_question, mode=mode, root_question=question
            )
            return assignment.specialist, output

        results = await asyncio.gather(*(_dispatch(a) for a in plan))
        specialist_outputs: dict[str, SpecialistOutput] = {
            name: output for pair in results if pair is not None for name, output in [pair]
        }

        general = GeneralSpecialist(self.llm, self.logger)
        review_report = await general.compare(specialist_outputs, question)

        # synthesize() returns a TeamDraft directly now — no adapter needed.
        return await self.synthesize(
            specialist_outputs, review_report, question, mode, team_plan=plan
        )

    async def balance(
        self,
        question: str,
        report_draft: ReportDraft,
        team_draft: TeamDraft,
    ) -> FinalAnswer:
        """Invoke the Balancing skill to merge the two drafts.

        The skill body holds the coverage-branch policy — Python never branches
        on coverage. If the LLM call is blocked, we degrade to a deterministic
        fallback: on coverage=="none" return the team draft verbatim with a
        one-line note; otherwise combine both answers with a flag.
        """
        user_message = (
            f"Reviewer question: {question}\n\n"
            f"=== Report draft (coverage = {report_draft.coverage}) ===\n"
            f"Answer: {report_draft.answer}\n"
            f"Evidence excerpts: {report_draft.evidence_excerpts}\n"
            f"Files consulted: {report_draft.files_consulted}\n\n"
            f"=== Team draft ===\n"
            f"Answer: {team_draft.answer}\n"
            f"Specialists consulted: {team_draft.specialists_consulted}\n"
            f"Open conflicts: "
            f"{[(c.pair, c.contradiction) for c in team_draft.open_conflicts]}\n"
            f"Cross-domain insights: {team_draft.cross_domain_insights}\n\n"
            "Merge per the Balancing skill's policy. Return JSON with answer + flags."
        )

        result = await self.llm.ainvoke(
            system_prompt=BALANCING_PROMPT,
            user_message=user_message,
        )

        if result.status == "blocked" or result.data is None:
            return self._balance_fallback(report_draft, team_draft)

        data = result.data
        answer = str(data.get("answer", "")).strip()
        flags = data.get("flags", []) or []
        if not isinstance(flags, list):
            flags = []

        if not answer:
            return self._balance_fallback(report_draft, team_draft)

        return FinalAnswer(
            answer=answer,
            flags=[str(f) for f in flags],
            report_draft=report_draft,
            team_draft=team_draft,
        )

    @staticmethod
    def _balance_fallback(
        report_draft: ReportDraft, team_draft: TeamDraft
    ) -> FinalAnswer:
        """Deterministic fallback when the Balancing LLM call is blocked or empty.

        Mirrors the policy in balancing.md so behavior stays predictable:
          - coverage=="none"  → team draft verbatim, prefixed with the no-reports note
          - coverage=="full"  → report answer, appended with team answer as supplement
          - coverage=="partial" → same shape as "full"; reviewer still gets both
        """
        if report_draft.coverage == "none":
            prefix = (
                "No prior curated reports were found for this case — answer is "
                "from live specialist analysis only.\n\n"
            )
            return FinalAnswer(
                answer=prefix + team_draft.answer,
                flags=["balancing fallback: LLM blocked on merge"],
                report_draft=report_draft,
                team_draft=team_draft,
            )

        merged = (
            f"[From curated reports]\n{report_draft.answer}\n\n"
            f"[From team specialists]\n{team_draft.answer}"
        )
        return FinalAnswer(
            answer=merged,
            flags=["balancing fallback: LLM blocked on merge"],
            report_draft=report_draft,
            team_draft=team_draft,
        )
