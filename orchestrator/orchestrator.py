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
)


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


class Orchestrator:
    """Coordinates synthesis of specialist outputs into a final answer."""

    def __init__(
        self,
        firewall: FirewallStack,
        logger: EventLogger,
        registry: SessionRegistry,
        pillar: str,
        pillar_config: dict | None = None,
    ):
        self.firewall = firewall
        self.logger = logger
        self.registry = registry
        self.pillar = pillar
        self.pillar_config = pillar_config or {}

    def synthesize(
        self,
        specialist_outputs: dict[str, SpecialistOutput],
        review_report: ReviewReport,
        question: str,
        mode: str,
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
        )
