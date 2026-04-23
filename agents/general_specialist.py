"""General Specialist — cross-domain reviewer with Compare skill."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from gateway.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import (
    Conflict,
    LLMResult,
    Resolution,
    ReviewReport,
    SpecialistOutput,
)
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


COMPARE_SYSTEM_PROMPT = _load_skill(_WORKFLOW_DIR / "comparison.md").body


class GeneralSpecialist:
    """Cross-domain reviewer that compares specialist outputs."""

    def __init__(self, llm: FirewalledModel, logger: EventLogger):
        self.llm = llm
        self.logger = logger

    async def compare(
        self,
        specialist_outputs: dict[str, SpecialistOutput],
        question: str,
    ) -> ReviewReport:
        if len(specialist_outputs) < 2:
            return ReviewReport()

        domains = list(specialist_outputs.keys())
        pairs = self._generate_pairs(domains)

        self.logger.log(
            "compare_start",
            {"domains": domains, "pairs": [list(p) for p in pairs], "question": question},
        )

        prompt_body = self._format_outputs_for_prompt(specialist_outputs)
        user_message = (
            f"Question: {question}\n\n"
            f"Specialist outputs:\n{prompt_body}\n\n"
            f"Pairs to compare: {[list(p) for p in pairs]}\n\n"
            "Identify contradictions, attempt resolution, and note cross-domain insights."
        )

        result = await self.llm.ainvoke(
            system_prompt=COMPARE_SYSTEM_PROMPT,
            user_message=user_message,
        )

        report = self._parse_compare_result(result)

        # Log events for each finding
        for r in report.resolved:
            self.logger.log("contradiction_found", {"pair": list(r.pair), "resolved": True})
            self.logger.log("question_raised", {"question": r.question_raised})
            self.logger.log("self_answer", {"answer": r.answer})
        for c in report.open_conflicts:
            self.logger.log("contradiction_found", {"pair": list(c.pair), "resolved": False})
            self.logger.log("question_raised", {"question": c.question_raised})

        return report

    def _generate_pairs(self, domains: list[str]) -> list[tuple[str, str]]:
        return list(itertools.combinations(sorted(domains), 2))

    def _format_outputs_for_prompt(
        self, outputs: dict[str, SpecialistOutput]
    ) -> str:
        parts = []
        for domain, output in outputs.items():
            parts.append(
                f"[{domain}]\n"
                f"Findings: {output.findings}\n"
                f"Evidence: {', '.join(output.evidence)}\n"
                f"Implications: {', '.join(output.implications)}\n"
            )
        return "\n".join(parts)

    @staticmethod
    def _as_str(v) -> str:
        """Coerce LLM output to string — LLMs sometimes return bool/None/etc for fields."""
        if v is None or v is False:
            return ""
        if v is True:
            return "true"
        if isinstance(v, str):
            return v
        return str(v)

    @staticmethod
    def _as_str_list(v) -> list[str]:
        if not v:
            return []
        if isinstance(v, list):
            return [GeneralSpecialist._as_str(x) for x in v if x]
        return [GeneralSpecialist._as_str(v)]

    def _parse_compare_result(self, result: LLMResult) -> ReviewReport:
        if result.status == "blocked" or result.data is None:
            return ReviewReport()

        data = result.data

        resolved = []
        for r in data.get("resolved", []):
            if not isinstance(r, dict):
                continue
            # Skip entries where there's no actual contradiction (LLM sometimes emits {"contradiction": false})
            contradiction = self._as_str(r.get("contradiction", ""))
            if not contradiction:
                continue
            pair = r.get("pair", ["", ""])
            if not isinstance(pair, list):
                pair = ["", ""]
            resolved.append(
                Resolution(
                    pair=tuple(self._as_str(p) for p in pair[:2]) if len(pair) >= 2 else ("", ""),
                    contradiction=contradiction,
                    question_raised=self._as_str(r.get("question_raised", "")),
                    answer=self._as_str(r.get("answer", "")),
                    supporting_evidence=self._as_str_list(r.get("supporting_evidence", [])),
                    conclusion=self._as_str(r.get("conclusion", "")),
                )
            )

        open_conflicts = []
        for c in data.get("open_conflicts", []):
            if not isinstance(c, dict):
                continue
            contradiction = self._as_str(c.get("contradiction", ""))
            if not contradiction:
                continue
            pair = c.get("pair", ["", ""])
            if not isinstance(pair, list):
                pair = ["", ""]
            open_conflicts.append(
                Conflict(
                    pair=tuple(self._as_str(p) for p in pair[:2]) if len(pair) >= 2 else ("", ""),
                    contradiction=contradiction,
                    question_raised=self._as_str(c.get("question_raised", "")),
                    reason_unresolved=self._as_str(c.get("reason_unresolved", "")),
                    evidence_from_both=self._as_str_list(c.get("evidence_from_both", [])),
                )
            )

        return ReviewReport(
            resolved=resolved,
            open_conflicts=open_conflicts,
            cross_domain_insights=self._as_str_list(data.get("cross_domain_insights", [])),
        )
