"""Base Specialist Agent — 3-step skill chain for domain analysis."""

from __future__ import annotations

from config.report_loader import get_specialist_prompt
from gateway.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import DomainSkill, LLMResult, SpecialistOutput
from tools.data_tools import list_available_tables, get_table_schema, query_table


BASE_INSTRUCTIONS = (
    "You are a specialist analyst. Follow these steps precisely:\n"
    "1. Identify the data you need and request it.\n"
    "2. Synthesise the data into findings.\n"
    "3. Produce a report or answer the question.\n"
    "\n"
    "═══ TIME & DATE DISCIPLINE (applies to EVERY specialist) ═══\n"
    "Many of your tables carry time/date columns in different shapes:\n"
    "  - ISO date:        2025-11-16          (e.g. payment_date, spend_date)\n"
    "  - ISO month:       2025-11             (e.g. month in txn_monthly)\n"
    "  - Month + year:    October'2024        (e.g. trans_month in model_scores)\n"
    "  - Year only:       2024\n"
    "Time-window reasoning is error-prone unless you follow these rules.\n"
    "\n"
    "1. ANCHOR TO THE CUT-OFF. Any word like 'recent', 'current', 'last N "
    "months', 'this year' is relative to the pillar DATA CUT-OFF DATE, NEVER "
    "relative to today's calendar date. Compute window bounds FIRST as "
    "explicit strings in the column's own format, then use them.\n"
    "   Example — cut-off 2025-12-01, 'last 3 months':\n"
    "     ISO column:        [2025-09-01, 2025-12-01]\n"
    "     'MonthName\\'YYYY': [September'2025, November'2025]\n"
    "\n"
    "2. USE RANGE FILTERS. `query_table` supports `filter_op`: one of\n"
    "   - 'eq' (default), 'ne', 'gt', 'gte', 'lt', 'lte'\n"
    "   - 'between' with filter_value='<low>,<high>' (inclusive both ends).\n"
    "   The filter knows how to compare ALL of the date formats above "
    "chronologically — you can pass 'October'2024' and 'December'2024' and it "
    "will order them correctly. You do NOT need to convert to ISO yourself; "
    "match the column's own format and the operators will work.\n"
    "   DO time-window filtering at query time:\n"
    "     query_table('payments', filter_column='payment_date',\n"
    "                 filter_op='between', filter_value='2025-09-01,2025-12-01',\n"
    "                 columns='payment_date,payment_amount,return_flag')\n"
    "     query_table('model_scores', filter_column='trans_month',\n"
    "                 filter_op='between', filter_value=\"September'2025,November'2025\",\n"
    "                 columns='trans_month,<score_cols>')\n"
    "   DON'T fetch all rows and filter mentally — that leads to date drift.\n"
    "\n"
    "3. CHECK THE COLUMN FORMAT FIRST. Before writing a filter_value, call "
    "`get_table_schema(table)` (or glance at what you already have) to see the "
    "date column's description. Match the filter_value to that format "
    "character-for-character. Mixing formats in one filter (e.g. an ISO low "
    "bound with a 'MonthName\\'YYYY' high bound) will NOT compare correctly.\n"
    "\n"
    "4. QUOTE DATES VERBATIM. When a date appears in a query result, copy the "
    "string exactly in your findings and evidence. Never paraphrase the year, "
    "month, or day. A row with payment_date='2024-09-24' must be cited as "
    "2024-09-24, never 2025-09-24. Re-check the year before every date "
    "citation.\n"
    "\n"
    "5. WHEN IN DOUBT, PROBE FIRST. If the table's date coverage is uncertain, "
    "run ONE unfiltered query with the date column in `columns=...` to see the "
    "actual span, then re-query with the right window.\n"
    "\n"
    "6. EMPTY WINDOW ≠ NO DATA. A filtered result of zero rows means 'no rows "
    "in THIS window'. Before reporting 'no X', re-check what IS the date "
    "coverage of the table for this case. Distinguish 'window empty' from "
    "'data absent'.\n"
)

_MAX_ROLLING_SUMMARY = 3000


class BaseSpecialistAgent:
    """A domain specialist that runs a 3-step LLM chain."""

    def __init__(
        self,
        domain_skill: DomainSkill,
        pillar_yaml: dict,
        firewall: FirewallStack,
        logger: EventLogger,
    ):
        self.skill = domain_skill
        self.pillar = pillar_yaml
        self.firewall = firewall
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

    def run(
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
        step1 = self.firewall.call(
            system_prompt=system_prompt,
            user_message=f"{question_header}\n\nWhat data do you need to answer your sub-question?",
            tools=tools,
        )
        self.logger.log("data_response", {"domain": self.skill.name, "status": step1.status})
        if step1.status == "blocked":
            return self._blocked_output(question, mode, "data_request", step1.error)

        # Step 2: Synthesise
        data_context = str(step1.data) if step1.data else "No data retrieved"
        step2 = self.firewall.call(
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

        step3 = self.firewall.call(
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
