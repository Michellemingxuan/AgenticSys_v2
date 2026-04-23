"""CLI entry point for the Agentic Credit Risk System."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from agents.guardrail_agent import GuardrailAgent
from agents.helper_tools import build_helper_tools
from agents.report_agent import ReportAgent
from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from gateway.llm_factory import build_llm
from logger.event_logger import EventLogger
from models.types import FinalAnswer
from orchestrator.chat_agent import ChatAgent
from orchestrator.orchestrator import Orchestrator
from tools.data_tools import init_tools


_RESULTS_DIR = Path(__file__).parent / "results"


async def run_question(
    question: str,
    pillar: str,
    llm,
    logger: EventLogger,
    registry: SessionRegistry,
    pillar_yaml: dict,
    case_id: str,
    catalog=None,
) -> FinalAnswer:
    """Entry point for a single reviewer question.

    Dispatches the Report Agent (reads curated `results/<case-id>/*.md`) and
    the team workflow in parallel, then merges via the Balancing skill.
    """
    orchestrator = Orchestrator(
        llm, logger, registry, pillar,
        pillar_config=pillar_yaml, catalog=catalog,
    )
    report_agent = ReportAgent(llm, logger)
    case_folder = _RESULTS_DIR / case_id

    return await orchestrator.run(question, case_folder, report_agent)


def _format_final_answer(final) -> str:
    """Minimal reviewer formatter for FinalAnswer (Phase 4).

    Future phases extend ChatAgent with a richer renderer; this is the
    simplest possible glue so the CLI stays runnable right after the
    parallel-pipeline cut-over.
    """
    parts = ["## Answer\n", final.answer]
    if final.flags:
        parts.append("\n## Flags")
        for flag in final.flags:
            parts.append(f"- {flag}")
    parts.append(
        f"\n## Provenance\n"
        f"- Report coverage: {final.report_draft.coverage}\n"
        f"- Files consulted: {final.report_draft.files_consulted or '(none)'}\n"
        f"- Specialists consulted: {final.team_draft.specialists_consulted or '(none)'}"
    )
    return "\n".join(parts)


async def amain():
    parser = argparse.ArgumentParser(description="Agentic Credit Risk Analysis System")
    parser.add_argument("--pillar", choices=["credit_risk", "escalation", "cbo"],
                        default="credit_risk")
    parser.add_argument("--question", type=str, default=None)
    parser.add_argument("--model", type=str, default="gpt-4.1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--case-id", type=str, default=None)
    args = parser.parse_args()

    session_id = str(uuid.uuid4())[:8]
    logger = EventLogger(session_id=session_id)
    logger.log("session_start", {"pillar": args.pillar, "model": args.model})

    firewall = FirewallStack(logger=logger)
    llm = build_llm(args.model, firewall)

    gen = DataGenerator(seed=args.seed, cases=50)
    gen.load_profiles()
    tables_raw = gen.generate_all()
    gateway = SimulatedDataGateway.from_generated(tables_raw)
    catalog = DataCatalog()
    init_tools(gateway, catalog, logger=logger)

    available_cases = gateway.list_case_ids()
    if not available_cases:
        print("No cases available. Check data generation.")
        sys.exit(1)

    case_id = args.case_id
    if case_id is None:
        print(f"\nAvailable cases ({len(available_cases)} total):")
        for cid in available_cases[:10]:
            print(f"  {cid}")
        if len(available_cases) > 10:
            print(f"  ... and {len(available_cases) - 10} more")
        case_id = available_cases[0]
        print(f"\nNo --case-id specified, using: {case_id}")
    elif case_id not in available_cases:
        print(f"Case '{case_id}' not found. Available: {', '.join(available_cases[:5])}...")
        sys.exit(1)

    gateway.set_case(case_id)
    logger.log("case_selected", {"case_id": case_id, "tables": gateway.list_tables()})

    pillar_loader = PillarLoader()
    pillar_yaml = pillar_loader.load(args.pillar) or {}

    registry = SessionRegistry()
    helper_tools = build_helper_tools()
    chat_agent = ChatAgent(llm, logger, tools=helper_tools)
    guardrail = GuardrailAgent(llm, logger)

    async def _screen_and_run(question: str) -> str:
        """Screen via Guardrail; if rejected, return the reason.
        Otherwise route the redacted question through run_question and format.
        """
        verdict = await guardrail.screen(question)
        if not verdict.passed:
            return f"[rejected] {verdict.reason}"
        final = await run_question(
            verdict.redacted_question, args.pillar,
            llm, logger, registry, pillar_yaml, case_id, catalog=catalog,
        )
        return _format_final_answer(final)

    if args.question:
        print(await _screen_and_run(args.question))
    else:
        print("Agentic Credit Risk System")
        print(f"Pillar: {args.pillar} | Model: {args.model}")
        print("Type 'quit' to exit.\n")

        loop = asyncio.get_running_loop()
        last_context = ""
        while True:
            try:
                question = (await loop.run_in_executor(None, input, ">> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break

            if not question:
                continue
            if question.lower() in ("quit", "exit", "q"):
                print("Goodbye.")
                break

            if question.startswith("/chat "):
                follow_up = question[6:].strip()
                response = await chat_agent.converse(follow_up, context=last_context)
                print(response)
                continue

            formatted = await _screen_and_run(question)
            if not formatted.startswith("[rejected]"):
                last_context = formatted
            print(formatted)
            print()

    logger.log("session_end", {})


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
