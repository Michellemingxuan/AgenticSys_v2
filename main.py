"""CLI entry point for the Agentic Credit Risk System."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from agents.general_specialist import GeneralSpecialist
from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from gateway.llm_factory import build_llm
from logger.event_logger import EventLogger
from models.types import FinalOutput
from orchestrator.chat_agent import ChatAgent
from orchestrator.orchestrator import Orchestrator
from skills.domain.loader import list_domain_skills, load_domain_skill
from tools.data_tools import init_tools


async def run_question(
    question: str,
    mode: str,
    pillar: str,
    llm,
    logger: EventLogger,
    registry: SessionRegistry,
    pillar_yaml: dict,
    catalog=None,
) -> FinalOutput:
    available = list_domain_skills()

    orchestrator = Orchestrator(
        llm, logger, registry, pillar,
        pillar_config=pillar_yaml, catalog=catalog,
    )

    active = registry.list_active()
    plan = await orchestrator.plan_team(
        question=question,
        available_specialists=available,
        active_specialists=active,
        mode=mode,
    )

    specialist_outputs = {}
    for assignment in plan:
        skill = load_domain_skill(assignment.specialist)
        if skill is None:
            continue
        agent = registry.get_or_create(
            domain=assignment.specialist,
            pillar=pillar,
            domain_skill=skill,
            pillar_yaml=pillar_yaml,
            llm=llm,
            logger=logger,
        )
        output = await agent.run(assignment.sub_question, mode=mode, root_question=question)
        specialist_outputs[assignment.specialist] = output

    general = GeneralSpecialist(llm, logger)
    review_report = await general.compare(specialist_outputs, question)

    final = await orchestrator.synthesize(
        specialist_outputs, review_report, question, mode, team_plan=plan,
    )
    return final


async def amain():
    parser = argparse.ArgumentParser(description="Agentic Credit Risk Analysis System")
    parser.add_argument("--pillar", choices=["credit_risk", "escalation", "cbo"],
                        default="credit_risk")
    parser.add_argument("--question", type=str, default=None)
    parser.add_argument("--mode", choices=["chat", "report"], default="chat")
    parser.add_argument("--model", type=str, default="gpt-4.1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--case-id", type=str, default=None)
    args = parser.parse_args()

    session_id = str(uuid.uuid4())[:8]
    logger = EventLogger(session_id=session_id)
    logger.log("session_start", {"pillar": args.pillar, "mode": args.mode, "model": args.model})

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
    chat_agent = ChatAgent(llm, logger)

    if args.question:
        final = await run_question(
            args.question, args.mode, args.pillar,
            llm, logger, registry, pillar_yaml, catalog=catalog,
        )
        print(chat_agent.format_for_reviewer(final))
    else:
        print("Agentic Credit Risk System")
        print(f"Pillar: {args.pillar} | Mode: {args.mode} | Model: {args.model}")
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

            final = await run_question(
                question, args.mode, args.pillar,
                llm, logger, registry, pillar_yaml, catalog=catalog,
            )
            formatted = chat_agent.format_for_reviewer(final)
            last_context = formatted
            print(formatted)
            print()

    logger.log("session_end", {})


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
