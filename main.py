"""CLI entry point for the Agentic Credit Risk System."""

from __future__ import annotations

import argparse
import sys
import uuid

from agents.general_specialist import GeneralSpecialist
from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import FinalOutput, ReviewReport
from orchestrator.chat_agent import ChatAgent
from orchestrator.orchestrator import Orchestrator
from orchestrator.team import TeamConstructor
from skills.domain.loader import list_domain_skills, load_domain_skill
from tools.data_tools import init_tools


def build_adapter(args):
    """Build the appropriate LLM adapter based on CLI args."""
    if args.use_env_pipeline:
        try:
            from gateway.safechain_adapter import SafeChainAdapter
            return SafeChainAdapter(llm=None, model_name=args.model)
        except Exception:
            raise NotImplementedError(
                "SafeChain adapter requires the safechain package. "
                "Use --model without --use-env-pipeline for OpenAI."
            )
    else:
        from gateway.openai_adapter import OpenAIAdapter
        return OpenAIAdapter(model=args.model)


def run_question(
    question: str,
    mode: str,
    pillar: str,
    firewall: FirewallStack,
    logger: EventLogger,
    registry: SessionRegistry,
    pillar_yaml: dict,
    catalog=None,
) -> FinalOutput:
    """Run the full pipeline for a single question."""
    available = list_domain_skills()

    # Team construction — pass catalog so LLM sees column names
    team_constructor = TeamConstructor(firewall, logger, catalog=catalog)
    active = registry.list_active()
    selected = team_constructor.select_specialists(
        question=question,
        pillar=pillar,
        available_specialists=available,
        active_specialists=active,
        mode=mode,
    )

    # Specialist dispatch
    specialist_outputs = {}
    for domain in selected:
        skill = load_domain_skill(domain)
        if skill is None:
            continue
        agent = registry.get_or_create(
            domain=domain,
            pillar=pillar,
            domain_skill=skill,
            pillar_yaml=pillar_yaml,
            firewall=firewall,
            logger=logger,
        )
        output = agent.run(question, mode=mode)
        specialist_outputs[domain] = output

    # Cross-domain comparison
    general = GeneralSpecialist(firewall, logger)
    review_report = general.compare(specialist_outputs, question)

    # Synthesis
    orchestrator = Orchestrator(firewall, logger, registry, pillar, pillar_config=pillar_yaml)
    final = orchestrator.synthesize(specialist_outputs, review_report, question, mode)

    return final


def main():
    parser = argparse.ArgumentParser(
        description="Agentic Credit Risk Analysis System"
    )
    parser.add_argument(
        "--pillar",
        choices=["credit_risk", "escalation", "cbo"],
        default="credit_risk",
        help="Analysis pillar (default: credit_risk)",
    )
    parser.add_argument(
        "--question",
        type=str,
        default=None,
        help="Single question (non-interactive mode)",
    )
    parser.add_argument(
        "--mode",
        choices=["chat", "report"],
        default="chat",
        help="Output mode (default: chat)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4.1",
        help="LLM model name (default: gpt-4.1)",
    )
    parser.add_argument(
        "--use-env-pipeline",
        action="store_true",
        help="Use SafeChain adapter for deployment environment",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for data generation (default: 42)",
    )
    parser.add_argument(
        "--case-id",
        type=str,
        default=None,
        help="Case ID to review (e.g. CASE-00001). If omitted, lists available cases.",
    )

    args = parser.parse_args()

    # Session setup
    session_id = str(uuid.uuid4())[:8]
    logger = EventLogger(session_id=session_id)
    logger.log("session_start", {"pillar": args.pillar, "mode": args.mode, "model": args.model})

    # Build adapter and firewall
    adapter = build_adapter(args)
    firewall = FirewallStack(adapter, logger)

    # Data generation — organized per case
    gen = DataGenerator(seed=args.seed, cases=50)
    gen.load_profiles()
    tables_raw = gen.generate_all()

    # Build per-case gateway from generated data
    gateway = SimulatedDataGateway.from_generated(tables_raw)
    catalog = DataCatalog()
    init_tools(gateway, catalog)

    # Case selection
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

    # Pillar config
    pillar_loader = PillarLoader()
    pillar_yaml = pillar_loader.load(args.pillar) or {}

    # Session registry
    registry = SessionRegistry()

    # Chat agent for formatting
    chat_agent = ChatAgent(firewall, logger)

    if args.question:
        # Single question mode
        final = run_question(
            args.question, args.mode, args.pillar,
            firewall, logger, registry, pillar_yaml,
            catalog=catalog,
        )
        formatted = chat_agent.format_for_reviewer(final)
        print(formatted)
    else:
        # Interactive mode
        print("Agentic Credit Risk System")
        print(f"Pillar: {args.pillar} | Mode: {args.mode} | Model: {args.model}")
        print("Type 'quit' to exit.\n")

        last_context = ""
        while True:
            try:
                question = input(">> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break

            if not question:
                continue
            if question.lower() in ("quit", "exit", "q"):
                print("Goodbye.")
                break

            if question.startswith("/chat "):
                # Follow-up conversation
                follow_up = question[6:].strip()
                response = chat_agent.converse(follow_up, context=last_context)
                print(response)
                continue

            final = run_question(
                question, args.mode, args.pillar,
                firewall, logger, registry, pillar_yaml,
                catalog=catalog,
            )
            formatted = chat_agent.format_for_reviewer(final)
            last_context = formatted
            print(formatted)
            print()

    logger.log("session_end", {})


if __name__ == "__main__":
    main()
