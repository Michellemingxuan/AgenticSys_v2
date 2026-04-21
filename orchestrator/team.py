"""Team construction — selects relevant specialists for a question."""

from __future__ import annotations

import json

from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from skills.domain.loader import load_domain_skill


TEAM_CONSTRUCTION_PROMPT = (
    "You are a team construction agent. Given a question and a description "
    "of each available specialist (including what data tables and columns "
    "they have access to), select the specialists that can answer the question.\n\n"
    "Guidelines:\n"
    "- Select specialists whose DATA contains fields relevant to the question.\n"
    "- Read each specialist's data coverage carefully — match the question to "
    "specific columns or tables.\n"
    "- Prefer warm specialists (already active with context) when relevant.\n"
    "- Be targeted, not exhaustive — but don't miss specialists with relevant data.\n"
    "- For broad questions (e.g. 'full report'), select all specialists.\n"
    "- Return a JSON object: {\"specialists\": [\"domain1\", \"domain2\"]}\n"
    "- Always return at least one specialist."
)


class TeamConstructor:
    """Selects relevant specialists for a given question."""

    def __init__(self, firewall: FirewallStack, logger: EventLogger,
                 catalog=None):
        self.firewall = firewall
        self.logger = logger
        self.catalog = catalog

    def select_specialists(
        self,
        question: str,
        pillar: str,
        available_specialists: list[str],
        active_specialists: list[dict],
        mode: str = "chat",
    ) -> list[str]:
        self.logger.log(
            "team_construction_start",
            {"question": question, "pillar": pillar, "mode": mode, "available": available_specialists},
        )

        # Report mode → all specialists (a full report needs every domain)
        if mode == "report":
            self.logger.log("team_construction_done", {"selected": available_specialists, "reason": "report mode — all specialists"})
            return available_specialists

        # Build specialist descriptions with data coverage
        spec_descriptions = self._build_specialist_descriptions(available_specialists)

        active_info = ""
        if active_specialists:
            active_info = "\nCurrently active (warm, have prior context):\n"
            for a in active_specialists:
                active_info += f"  - {a['domain']} ({a['questions_answered']} questions answered)\n"

        user_message = (
            f"Question: {question}\n"
            f"Pillar: {pillar}\n\n"
            f"Available specialists and their data:\n{spec_descriptions}\n"
            f"{active_info}\n"
            "Select the relevant specialists for this question."
        )

        result = self.firewall.call(
            system_prompt=TEAM_CONSTRUCTION_PROMPT,
            user_message=user_message,
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("team_construction_fallback", {"reason": "blocked"})
            return available_specialists

        selected = self._parse_selection(result.data, available_specialists)

        self.logger.log("team_construction_done", {"selected": selected})
        return selected

    def _build_specialist_descriptions(self, available: list[str]) -> str:
        """Build a description of each specialist including what data they access."""
        lines = []
        for domain in available:
            skill = load_domain_skill(domain)
            if skill is None:
                lines.append(f"- {domain}: (no skill loaded)")
                continue

            # Domain expertise summary
            desc = f"- {domain}:"
            desc += f"\n    Focus: {skill.system_prompt[:150]}"
            desc += f"\n    Tables: {', '.join(skill.data_hints)}"

            # Add column names from catalog if available
            if self.catalog:
                for table in skill.data_hints:
                    schema = self.catalog.get_schema(table)
                    if schema:
                        col_names = list(schema.keys())
                        # Show first 15 columns to keep prompt manageable
                        if len(col_names) > 15:
                            desc += f"\n    Columns ({table}): {', '.join(col_names[:15])}... (+{len(col_names)-15} more)"
                        else:
                            desc += f"\n    Columns ({table}): {', '.join(col_names)}"

            desc += f"\n    Risk signals: {', '.join(skill.risk_signals[:3])}"
            lines.append(desc)

        return "\n".join(lines)

    def _parse_selection(
        self, data: dict, available: list[str]
    ) -> list[str]:
        raw = data.get("specialists", data.get("response", ""))

        if isinstance(raw, list):
            names = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                names = parsed.get("specialists", [])
            except (json.JSONDecodeError, AttributeError):
                return available
        else:
            return available

        # Validate against available list
        validated = [n for n in names if n in available]
        if not validated:
            return available

        return validated
