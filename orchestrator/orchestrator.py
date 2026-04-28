"""Orchestrator — routes questions through the SDK agent graph and returns a FinalAnswer."""

from __future__ import annotations

from pathlib import Path

from agents import Runner
from agents.exceptions import AgentsException
from agents.items import ToolCallOutputItem

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
        try:
            result = await Runner.run(self.orchestrator_agent, question, context=ctx)
            final = redact_payload(result.final_output)
        except AgentsException as exc:
            self.logger.log("orchestrator_run_blocked",
                            {"exc_type": type(exc).__name__, "message": str(exc)})
            final = self._trace_extraction_fallback(exc)
        self.logger.log("orchestrator_run_done",
                        {"flag_count": len(final.flags),
                         "answer_len": len(final.answer)})
        return final

    def _trace_extraction_fallback(self, exc: AgentsException) -> FinalAnswer:
        """Recover any completed tool outputs from a failed Runner.run.

        Walks ``exc.run_data.new_items`` for ToolCallOutputItems and stitches
        their (already-deserialized) outputs back into a FinalAnswer. This is
        the β behavior — preserves the user's view of what specialists DID
        produce before the orchestrator was blocked.
        """
        items = getattr(getattr(exc, "run_data", None), "new_items", []) or []
        # Duck-type check for ToolCallOutputItem since isinstance may not work
        # with all mock configurations in tests.
        completed = [
            i for i in items
            if isinstance(i, ToolCallOutputItem)
            or (hasattr(i, "output") and hasattr(i, "agent"))
        ]

        report_draft = None
        specialist_outputs: list = []
        for item in completed:
            agent_name = getattr(getattr(item, "agent", None), "name", None)
            out = item.output
            if agent_name == "report_agent":
                report_draft = out
            elif agent_name == "general_specialist":
                # General specialist's review is informational; skip in fallback
                continue
            else:
                specialist_outputs.append((agent_name, out))

        if report_draft is None and not specialist_outputs:
            return FinalAnswer(
                answer="Analysis was blocked by content firewall after retries.",
                flags=["orchestrator blocked, no partial drafts recovered"],
            )

        # Stitch a coverage-aware fallback answer (mirrors today's _balance_fallback).
        parts: list[str] = []
        if report_draft is not None:
            # report_draft may be a ReportDraft Pydantic instance OR (defensively) a dict
            report_answer = getattr(report_draft, "answer", None) or (
                report_draft.get("answer") if isinstance(report_draft, dict) else ""
            )
            coverage = getattr(report_draft, "coverage", None) or (
                report_draft.get("coverage") if isinstance(report_draft, dict) else "none"
            )
            if coverage != "none" and report_answer:
                parts.append(f"[From curated reports]\n{report_answer}")

        if specialist_outputs:
            spec_chunks = []
            for name, out in specialist_outputs:
                findings = getattr(out, "findings", None) or (
                    out.get("findings") if isinstance(out, dict) else ""
                )
                if findings:
                    spec_chunks.append(f"  • {name}: {findings}")
            if spec_chunks:
                parts.append("[From team specialists]\n" + "\n".join(spec_chunks))

        if not parts:
            return FinalAnswer(
                answer="Analysis was blocked by content firewall after retries.",
                flags=["orchestrator blocked, no partial drafts recovered"],
            )

        return FinalAnswer(
            answer="\n\n".join(parts),
            flags=["balancing fallback: orchestrator blocked"],
        )
