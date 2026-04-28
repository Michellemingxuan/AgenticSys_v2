"""Orchestrator — routes questions through the SDK agent graph and returns a FinalAnswer."""

from __future__ import annotations

from pathlib import Path

from agents import Runner

from case_agents.app_context import AppContext
from case_agents.orchestrator_agent import build_orchestrator_agent
from case_agents.report_agent import build_report_agent
from case_agents.general_specialist import build_general_specialist
from case_agents.specialist_agent import build_specialist_agent
from llm.firewall_stack import redact_payload
from logger.event_logger import EventLogger
from models.types import FinalAnswer
from skills.domain.loader import list_domain_skills, load_domain_skill


class Orchestrator:
    """Coordinates the SDK agent graph and returns a FinalAnswer."""

    def __init__(
        self,
        llm,                  # legacy — ignored when clients is provided
        logger: EventLogger,
        registry=None,        # legacy — ignored when clients is provided
        pillar: str = "credit",
        pillar_config: dict | None = None,
        catalog=None,
        gateway=None,
        clients=None,
    ):
        self.llm = llm
        self.logger = logger
        self.registry = registry
        self.pillar = pillar
        self.pillar_config = pillar_config or {}
        self.catalog = catalog
        self.gateway = gateway
        self.clients = clients

        # Build the SDK agent graph if clients are provided. (When legacy
        # callers haven't been updated yet, agent graph is None and calling
        # .run() will error — fixed in Task 5.1 once main.py is migrated.)
        if clients is not None:
            domain_names = list_domain_skills()
            specialists = [
                build_specialist_agent(load_domain_skill(d), self.pillar_config,
                                       model=clients.model)
                for d in domain_names if load_domain_skill(d) is not None
            ]
            self.report_agent_obj = build_report_agent(model=clients.model)
            self.general_agent = build_general_specialist(model=clients.model)
            self.orchestrator_agent = build_orchestrator_agent(
                specialists=specialists,
                report_agent=self.report_agent_obj,
                general_specialist=self.general_agent,
                model=clients.model,
            )
        else:
            self.orchestrator_agent = None

    async def run(
        self,
        question: str,
        case_folder: Path,
        report_agent=None,  # legacy arg — ignored under SDK path
    ) -> FinalAnswer:
        """Route the question through the SDK agent graph and return a FinalAnswer."""
        self.logger.log("orchestrator_run_start",
                        {"question": question, "case_folder": str(case_folder)})
        ctx = AppContext(gateway=self.gateway, case_folder=case_folder, logger=self.logger)
        result = await Runner.run(self.orchestrator_agent, question, context=ctx)
        final = redact_payload(result.final_output)
        self.logger.log("orchestrator_run_done",
                        {"flag_count": len(final.flags),
                         "answer_len": len(final.answer)})
        return final
