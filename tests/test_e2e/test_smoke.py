"""End-to-end smoke tests for the full pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from case_agents.session_registry import SessionRegistry
from logger.event_logger import EventLogger
from models.types import LLMResult
from skills.domain.loader import load_domain_skill


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="smoke-test", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success", data={"response": "Default mock response"}
    ))
    return llm


def test_specialist_reuse_across_questions(mock_llm, logger):
    """Verify that registry reuses specialist instances across questions."""
    registry = SessionRegistry()
    skill = load_domain_skill("bureau")
    assert skill is not None

    agent1 = registry.get_or_create(
        domain="bureau",
        pillar="credit_risk",
        domain_skill=skill,
        pillar_yaml={},
        llm=mock_llm,
        logger=logger,
    )

    agent1._update_rolling_summary("Q1", "Score is 720")
    assert agent1.rolling_summary != ""

    agent2 = registry.get_or_create(
        domain="bureau",
        pillar="credit_risk",
        domain_skill=skill,
        pillar_yaml={},
        llm=mock_llm,
        logger=logger,
    )

    assert agent1 is agent2
    assert "Score is 720" in agent2.rolling_summary
