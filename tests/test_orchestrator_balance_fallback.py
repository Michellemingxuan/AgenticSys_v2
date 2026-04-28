"""Verifies β trace-extraction fallback when Runner.run raises."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import AsyncOpenAI

from agents.exceptions import MaxTurnsExceeded
from agents.items import ToolCallOutputItem
from llm.factory import build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import FinalAnswer, ReportDraft, SpecialistOutput
from orchestrator.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_balance_fallback_recovers_partial_drafts(tmp_path):
    logger = EventLogger(session_id="t")
    firewall = FirewallStack(logger, max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=MagicMock(spec=AsyncOpenAI))
    orch = Orchestrator(
        llm=None, logger=logger, registry=None, pillar="credit",
        pillar_config={}, catalog=MagicMock(), gateway=MagicMock(),
        clients=clients,
    )

    # Simulate that during the orchestrator's loop, the report_agent tool
    # completed (returned a ReportDraft) and one specialist tool completed
    # (returned a SpecialistOutput), but then the orchestrator's final
    # synthesis turn was blocked, raising MaxTurnsExceeded.
    report_draft = ReportDraft(
        answer="Prior report says X", coverage="full",
        evidence_excerpts=[], files_consulted=["report.md"],
    )
    specialist_output = SpecialistOutput(
        domain="credit", question="Is this case high risk?", mode="chat",
        findings="Risk score is elevated", evidence=["FICO 540"],
        implications=["high default likelihood"], data_gaps=[],
    )

    # Build mock ToolCallOutputItems. The agent.name links them to which tool fired.
    report_agent_mock = MagicMock(); report_agent_mock.name = "report_agent"
    specialist_agent_mock = MagicMock(); specialist_agent_mock.name = "creditrisk"

    item1 = MagicMock(spec=ToolCallOutputItem)
    item1.output = report_draft
    item1.agent = report_agent_mock
    item2 = MagicMock(spec=ToolCallOutputItem)
    item2.output = specialist_output
    item2.agent = specialist_agent_mock

    fake_run_data = MagicMock()
    fake_run_data.new_items = [item1, item2]

    exc = MaxTurnsExceeded("simulated turn exhaustion")
    exc.run_data = fake_run_data

    with patch("orchestrator.orchestrator.Runner.run", new=AsyncMock(side_effect=exc)):
        result = await orch.run(question="q", case_folder=tmp_path, report_agent=None)

    assert isinstance(result, FinalAnswer)
    # Should contain content from BOTH the report draft AND the specialist
    assert "Prior report says X" in result.answer
    assert "Risk score is elevated" in result.answer
    assert any("balancing fallback" in f for f in result.flags)


@pytest.mark.asyncio
async def test_balance_fallback_no_partials_returns_blocked_message(tmp_path):
    """When the orchestrator failed before any tool completed, return a clear blocked message."""
    logger = EventLogger(session_id="t")
    firewall = FirewallStack(logger, max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=MagicMock(spec=AsyncOpenAI))
    orch = Orchestrator(
        llm=None, logger=logger, registry=None, pillar="credit",
        pillar_config={}, catalog=MagicMock(), gateway=MagicMock(),
        clients=clients,
    )

    fake_run_data = MagicMock()
    fake_run_data.new_items = []  # nothing completed
    exc = MaxTurnsExceeded("simulated")
    exc.run_data = fake_run_data

    with patch("orchestrator.orchestrator.Runner.run", new=AsyncMock(side_effect=exc)):
        result = await orch.run(question="q", case_folder=tmp_path, report_agent=None)

    assert isinstance(result, FinalAnswer)
    assert "blocked" in result.answer.lower()
    assert any("orchestrator blocked" in f for f in result.flags)
