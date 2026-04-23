"""Base Specialist Agent — 3-step skill chain for domain analysis."""

from __future__ import annotations

from pathlib import Path

from config.report_loader import get_specialist_prompt
from gateway.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import DomainSkill, LLMResult, SpecialistOutput
from skills.loader import load_skill as _load_skill
from tools.data_tools import list_available_tables, get_table_schema, query_table


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


BASE_INSTRUCTIONS = _load_skill(_WORKFLOW_DIR / "data_query.md").body

_MAX_ROLLING_SUMMARY = 3000


class BaseSpecialistAgent:
    """A domain specialist that runs a 3-step LLM chain."""

    def __init__(
        self,
        domain_skill: DomainSkill,
        pillar_yaml: dict,
        llm: FirewalledModel,
        logger: EventLogger,
    ):
        self.skill = domain_skill
        self.pillar = pillar_yaml
        self.llm = llm
        self.logger = logger
        self.rolling_summary: str = ""
        self._questions_answered: list[str] = []

    @property
    def questions_answered(self) -> int:
        return len(self._questions_answered)

    def _build_system_prompt(self) -> str:
        parts = [BASE_INSTRUCTIONS]

        # Domain skill context
        parts.append(f"Domain: {self.skill.name}")
        parts.append(f"Expertise: {self.skill.system_prompt}")
        if self.skill.data_hints:
            parts.append(f"Data hints: {', '.join(self.skill.data_hints)}")
        if self.skill.interpretation_guide:
            parts.append(f"Interpretation guide: {self.skill.interpretation_guide}")
        if self.skill.risk_signals:
            parts.append(f"Risk signals: {', '.join(self.skill.risk_signals)}")

        # Pillar context
        if self.pillar:
            if "focus" in self.pillar:
                parts.append(f"Pillar focus: {self.pillar['focus']}")
            if "overlay" in self.pillar:
                parts.append(f"Pillar overlay: {self.pillar['overlay']}")
            if "cut_off_date" in self.pillar:
                cutoff = self.pillar["cut_off_date"]
                parts.append(
                    f"DATA CUT-OFF DATE: {cutoff}\n"
                    f"CRITICAL — Interpret ALL time-window language ('recent', "
                    f"'current', 'last 3 months', 'this year') relative to this "
                    f"cut-off, NEVER relative to today's calendar date.\n"
                    f"Example: 'recent 3 months' means the three months ending on "
                    f"{cutoff} (i.e., roughly the 3 months immediately preceding it), "
                    f"NOT the 3 months before today. No data exists beyond {cutoff}."
                )

        # Rolling summary
        if self.rolling_summary:
            parts.append(f"Previous analysis:\n{self.rolling_summary}")

        return "\n\n".join(parts)

    def _update_rolling_summary(self, question: str, findings: str) -> None:
        entry = f"Q: {question}\nA: {findings}\n---\n"
        self.rolling_summary += entry
        if len(self.rolling_summary) > _MAX_ROLLING_SUMMARY:
            self.rolling_summary = self.rolling_summary[-_MAX_ROLLING_SUMMARY:]

    async def run(
        self,
        question: str,
        mode: str = "chat",
        root_question: str | None = None,
    ) -> SpecialistOutput:
        """Run the 3-step specialist chain.

        Args:
            question: the (sub-)question this specialist is responsible for.
            mode: "chat" or "report".
            root_question: the reviewer's original question, if it was
                decomposed into sub-questions by the orchestrator. When
                provided and different from ``question``, it is included in
                prompts as context so the specialist understands how its
                sub-question relates to the broader ask.
        """
        system_prompt = self._build_system_prompt()
        tools = [list_available_tables, get_table_schema, query_table]

        if root_question and root_question != question:
            question_header = (
                f"Root question (reviewer's original ask): {root_question}\n"
                f"Your sub-question: {question}"
            )
        else:
            question_header = f"Question: {question}"

        # Step 1: Data request
        self.logger.log(
            "data_request",
            {"domain": self.skill.name, "question": question, "root_question": root_question},
        )
        step1 = await self.llm.ainvoke(
            system_prompt=system_prompt,
            user_message=f"{question_header}\n\nWhat data do you need to answer your sub-question?",
            tools=tools,
        )
        self.logger.log("data_response", {"domain": self.skill.name, "status": step1.status})
        if step1.status == "blocked":
            return self._blocked_output(question, mode, "data_request", step1.error)

        # Step 2: Synthesise
        data_context = str(step1.data) if step1.data else "No data retrieved"
        step2 = await self.llm.ainvoke(
            system_prompt=system_prompt,
            user_message=(
                f"{question_header}\n\n"
                f"Based on the following data, synthesise findings for your sub-question.\n\n"
                f"Data: {data_context}"
            ),
        )
        self.logger.log("synthesis", {"domain": self.skill.name, "status": step2.status})
        if step2.status == "blocked":
            return self._blocked_output(question, mode, "synthesis", step2.error)

        # Step 3: Report or answer — uses templates from config/prompts/{report,chat}.yaml
        # Assembles: common format + mode instructions + pillar-specific instructions
        findings = str(step2.data) if step2.data else "No findings"
        pillar_instructions = self.pillar.get("report_instructions", "")
        step3_msg = get_specialist_prompt(
            mode=mode,
            question=question,
            findings=findings,
            domain=self.skill.name,
            pillar_report_instructions=pillar_instructions,
        )
        if root_question and root_question != question:
            step3_msg = (
                f"Root question (reviewer's original ask): {root_question}\n\n"
                + step3_msg
            )

        step3 = await self.llm.ainvoke(
            system_prompt=system_prompt,
            user_message=step3_msg,
        )
        event_type = "report_generated" if mode == "report" else "answer_generated"
        self.logger.log(event_type, {"domain": self.skill.name, "status": step3.status})
        if step3.status == "blocked":
            return self._blocked_output(question, mode, event_type, step3.error)

        # Build output
        output_data = step3.data or {}
        output = SpecialistOutput(
            domain=self.skill.name,
            question=question,
            mode=mode,
            findings=output_data.get("findings", findings),
            evidence=output_data.get("evidence", []),
            implications=output_data.get("implications", []),
            data_gaps=output_data.get("data_gaps", []),
            raw_data=step1.data or {},
        )

        self._update_rolling_summary(question, output.findings)
        self._questions_answered.append(question)
        return output

    def _blocked_output(
        self, question: str, mode: str, step: str, error: str | None
    ) -> SpecialistOutput:
        return SpecialistOutput(
            domain=self.skill.name,
            question=question,
            mode=mode,
            findings=f"Analysis incomplete — blocked at {step}: {error or 'unknown'}",
            evidence=[],
            implications=[],
            data_gaps=[],
            raw_data={},
        )
