"""Session registry — reuses specialist agents across questions."""

from __future__ import annotations

from case_agents.base_agent import BaseSpecialistAgent
from llm.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import DomainSkill


class SessionRegistry:
    """Tracks and reuses specialist agents within a session."""

    def __init__(self):
        self._active: dict[tuple[str, str], BaseSpecialistAgent] = {}

    def get_or_create(
        self,
        domain: str,
        pillar: str,
        domain_skill: DomainSkill,
        pillar_yaml: dict,
        llm: FirewalledModel,
        logger: EventLogger,
    ) -> BaseSpecialistAgent:
        key = (domain, pillar)
        if key in self._active:
            logger.log("specialist_reused", {"domain": domain, "pillar": pillar})
            return self._active[key]

        agent = BaseSpecialistAgent(domain_skill, pillar_yaml, llm, logger)
        self._active[key] = agent
        logger.log("specialist_invoked", {"domain": domain, "pillar": pillar})
        return agent

    def list_active(self) -> list[dict]:
        result = []
        for (domain, pillar), agent in self._active.items():
            result.append({
                "domain": domain,
                "pillar": pillar,
                "questions_answered": agent.questions_answered,
                "summary_preview": agent.rolling_summary[:200] if agent.rolling_summary else "",
            })
        return result

    def clear(self) -> None:
        self._active.clear()
