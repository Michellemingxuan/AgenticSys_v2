"""End-to-end smoke tests for the full pipeline."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.general_specialist import GeneralSpecialist
from agents.session_registry import SessionRegistry
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import FinalOutput, LLMResult, ReviewReport, SpecialistOutput
from orchestrator.chat_agent import ChatAgent
from orchestrator.orchestrator import Orchestrator
from skills.domain.loader import list_domain_skills, load_domain_skill
from tools.data_tools import init_tools


def _mock_firewall_call(system_prompt: str, user_message: str, **kwargs) -> LLMResult:
    """Route mock responses based on prompt content."""
    combined = (system_prompt + " " + user_message).lower()

    # Step 1 of plan_team → team selection
    if "team selection step" in combined or (
        "pick the specialists" in combined and "plan" not in user_message.lower()
    ):
        return LLMResult(
            status="success",
            data={"specialists": ["bureau", "spend_payments"]},
        )

    # Step 2 of plan_team → sub-question decomposition
    if "sub-question decomposition step" in combined or "one sub-question per specialist" in combined:
        return LLMResult(
            status="success",
            data={
                "plan": [
                    {"specialist": "bureau", "sub_question": "What does bureau data say?"},
                    {"specialist": "spend_payments", "sub_question": "What do spend/payment data say?"},
                ]
            },
        )

    if "data_request" in combined or "what data do you need" in combined:
        return LLMResult(
            status="success",
            data={
                "intent": "test",
                "variables": ["score"],
                "table_hints": ["bureau_full"],
            },
        )

    if "synthesise" in combined or "synthesize" in combined or "synthesis" in combined:
        # Check if this is the orchestrator synthesize or specialist synthesis
        if "merge" in combined or "orchestrator" in combined or "unified answer" in combined:
            return LLMResult(
                status="success",
                data={
                    "answer": "Final answer based on bureau and spend analysis.",
                    "data_gap_assessments": [],
                },
            )
        return LLMResult(
            status="success",
            data={
                "findings": "Test findings from synthesis",
                "evidence": ["e1"],
                "implications": ["i1"],
                "data_gaps": [],
            },
        )

    if "answer" in combined and ("concisely" in combined or "question" in combined):
        return LLMResult(
            status="success",
            data={
                "answer": "Test answer",
                "findings": "Test findings",
                "evidence": ["e1"],
            },
        )

    if "report" in combined and "detailed" in combined:
        return LLMResult(
            status="success",
            data={
                "findings": "Detailed report findings",
                "evidence": ["e1"],
                "implications": ["i1"],
            },
        )

    if "pairwise" in combined or "contradict" in combined or "cross-domain" in combined:
        return LLMResult(
            status="success",
            data={
                "resolved": [],
                "open_conflicts": [],
                "cross_domain_insights": ["insight1"],
            },
        )

    # Default response
    return LLMResult(
        status="success",
        data={"response": "Default mock response"},
    )


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="smoke-test", log_dir=str(tmp_path))


@pytest.fixture
def mock_firewall(logger):
    adapter = MagicMock()
    fw = FirewallStack(adapter, logger)
    fw.call = MagicMock(side_effect=_mock_firewall_call)
    return fw


def test_full_pipeline_smoke(mock_firewall, logger, tmp_path):
    """Full pipeline: team construction -> specialist dispatch -> compare -> synthesize -> format."""
    # Data setup — per-case gateway
    gen = DataGenerator(seed=42)
    gen.load_profiles()
    tables_raw = gen.generate_all()

    gateway = SimulatedDataGateway.from_generated(tables_raw)
    # Set a case for the test
    case_ids = gateway.list_case_ids()
    assert len(case_ids) > 0
    gateway.set_case(case_ids[0])

    catalog = DataCatalog()
    init_tools(gateway, catalog)

    registry = SessionRegistry()
    pillar = "credit_risk"
    question = "What is the overall credit risk for this applicant?"

    # Step 1: Team planning (specialist selection + sub-question decomposition)
    orchestrator = Orchestrator(mock_firewall, logger, registry, pillar)
    available = list_domain_skills()
    plan = orchestrator.plan_team(
        question=question,
        available_specialists=available,
        active_specialists=[],
    )
    assert len(plan) >= 1

    # Step 2: Specialist dispatch
    specialist_outputs = {}
    for assignment in plan:
        skill = load_domain_skill(assignment.specialist)
        if skill is None:
            continue
        agent = registry.get_or_create(
            domain=assignment.specialist,
            pillar=pillar,
            domain_skill=skill,
            pillar_yaml={},
            firewall=mock_firewall,
            logger=logger,
        )
        output = agent.run(assignment.sub_question, mode="chat", root_question=question)
        specialist_outputs[assignment.specialist] = output

    assert len(specialist_outputs) >= 1

    # Step 3: Compare
    general = GeneralSpecialist(mock_firewall, logger)
    review_report = general.compare(specialist_outputs, question)
    assert isinstance(review_report, ReviewReport)

    # Step 4: Synthesize — reuse the orchestrator instance from Step 1.
    final = orchestrator.synthesize(
        specialist_outputs, review_report, question, "chat", team_plan=plan,
    )

    assert isinstance(final, FinalOutput)
    assert len(final.answer) > 0
    assert len(final.specialists_consulted) >= 1

    # Step 5: Format
    chat_agent = ChatAgent(mock_firewall, logger)
    formatted = chat_agent.format_for_reviewer(final)
    assert len(formatted) > 0
    assert "Specialists consulted" in formatted


def test_specialist_reuse_across_questions(mock_firewall, logger):
    """Verify that registry reuses specialist instances across questions."""
    registry = SessionRegistry()
    skill = load_domain_skill("bureau")
    assert skill is not None

    # Create specialist via registry
    agent1 = registry.get_or_create(
        domain="bureau",
        pillar="credit_risk",
        domain_skill=skill,
        pillar_yaml={},
        firewall=mock_firewall,
        logger=logger,
    )

    # Simulate a rolling summary update
    agent1._update_rolling_summary("Q1", "Score is 720")
    assert agent1.rolling_summary != ""

    # Get again via registry — should be the same instance
    agent2 = registry.get_or_create(
        domain="bureau",
        pillar="credit_risk",
        domain_skill=skill,
        pillar_yaml={},
        firewall=mock_firewall,
        logger=logger,
    )

    assert agent1 is agent2
    assert "Score is 720" in agent2.rolling_summary
