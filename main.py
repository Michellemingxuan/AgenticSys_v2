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
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from datalayer.generator import DataGenerator
from llm.firewall_stack import FirewallStack
from llm.factory import build_llm
from logger.event_logger import EventLogger
from models.types import FinalAnswer
from orchestrator.chat_agent import ChatAgent
from orchestrator.orchestrator import Orchestrator
from tools.data_tools import init_tools


_REPORTS_DIR = Path(__file__).parent / "reports"
_DATA_TABLES_DIR = Path(__file__).parent / "data_tables"


def _resolve_data_source(flag: str, tables_dir: Path) -> tuple[str, Path | None]:
    """Pick where case data comes from.

    Args:
        flag: one of "auto", "real", "simulated", "generator".
        tables_dir: root of the data_tables/ folder.

    Returns:
        (source_name, csv_dir) where csv_dir is None for the generator path.
        Raises SystemExit if the user explicitly asked for real/simulated
        and that folder is empty.
    """
    real_dir = tables_dir / "real"
    sim_dir = tables_dir / "simulated"

    def _has_cases(p: Path) -> bool:
        return p.is_dir() and any(c.is_dir() for c in p.iterdir())

    if flag == "generator":
        return "generator", None
    if flag == "real":
        if not _has_cases(real_dir):
            raise SystemExit(f"--data-source real requested but {real_dir} is empty")
        return "real", real_dir
    if flag == "simulated":
        if not _has_cases(sim_dir):
            raise SystemExit(f"--data-source simulated requested but {sim_dir} is empty")
        return "simulated", sim_dir
    # auto
    if _has_cases(real_dir):
        return "real", real_dir
    if _has_cases(sim_dir):
        return "simulated", sim_dir
    return "generator", None


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

    Dispatches the Report Agent (reads curated `reports/<case-id>/*.md`) and
    the team workflow in parallel, then merges via the Balancing skill.
    """
    orchestrator = Orchestrator(
        llm, logger, registry, pillar,
        pillar_config=pillar_yaml, catalog=catalog,
    )
    report_agent = ReportAgent(llm, logger)
    case_folder = _REPORTS_DIR / case_id

    return await orchestrator.run(question, case_folder, report_agent)


async def amain():
    parser = argparse.ArgumentParser(description="Agentic Credit Risk Analysis System")
    parser.add_argument("--pillar", choices=["credit_risk", "escalation", "cbo"],
                        default="credit_risk")
    parser.add_argument("--question", type=str, default=None)
    parser.add_argument("--model", type=str, default="gpt-4.1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--case-id", type=str, default=None)
    parser.add_argument(
        "--data-source",
        choices=["auto", "real", "simulated", "generator"],
        default="auto",
        help="Where to load case data from. 'auto' resolves to real → simulated → generator.",
    )
    args = parser.parse_args()

    session_id = str(uuid.uuid4())[:8]
    logger = EventLogger(session_id=session_id)
    logger.log("session_start", {"pillar": args.pillar, "model": args.model})

    firewall = FirewallStack(logger=logger)
    llm = build_llm(args.model, firewall)

    source, csv_dir = _resolve_data_source(args.data_source, _DATA_TABLES_DIR)
    if source == "generator":
        gen = DataGenerator(seed=args.seed, cases=50)
        gen.load_profiles()
        tables_raw = gen.generate_all()
        gateway = LocalDataGateway.from_generated(tables_raw)
        logger.log("data_source", {"source": "generator", "path": None,
                                   "case_count": len(gateway.list_case_ids())})
    else:
        gateway = LocalDataGateway.from_case_folders(str(csv_dir))
        logger.log("data_source", {"source": source, "path": str(csv_dir),
                                   "case_count": len(gateway.list_case_ids())})

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
        return chat_agent.format_final_answer(final)

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
