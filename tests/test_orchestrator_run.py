"""End-to-end smoke: real Orchestrator.run, mocked Runner.run."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import AsyncOpenAI

from llm.factory import build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import FinalAnswer
from orchestrator.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_orchestrator_run_returns_final_answer(tmp_path):
    logger = EventLogger(session_id="t")
    firewall = FirewallStack(logger, max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=MagicMock(spec=AsyncOpenAI))

    orch = Orchestrator(
        llm=None, logger=logger, registry=None, pillar="credit",
        pillar_config={}, catalog=MagicMock(), gateway=MagicMock(),
        clients=clients,
    )

    fake_answer = FinalAnswer(answer="Synthesized answer.", flags=[])
    fake_result = MagicMock()
    fake_result.final_output = fake_answer

    with patch("orchestrator.orchestrator.Runner.run", new=AsyncMock(return_value=fake_result)):
        result = await orch.run(
            question="Is this case high risk?",
            case_folder=tmp_path,
            report_agent=None,  # legacy arg — ignored under new path
        )

    assert isinstance(result, FinalAnswer)
    assert result.answer == "Synthesized answer."
