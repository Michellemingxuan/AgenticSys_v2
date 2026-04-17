"""Tests for agents.base_agent."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.base_agent import BaseSpecialistAgent, BASE_INSTRUCTIONS
from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import DomainSkill, LLMResult, SpecialistOutput


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def bureau_skill():
    return DomainSkill(
        name="bureau",
        system_prompt="You are a bureau analyst.",
        data_hints=["bureau_full"],
        interpretation_guide="Flag 90D+ delinquencies",
        risk_signals=["Delinquency Risk", "Flag 90D+"],
    )


@pytest.fixture
def pillar_yaml():
    return {
        "focus": "Delinquency Risk",
        "overlay": "Flag 90D+",
    }


@pytest.fixture
def mock_firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter, logger)


def test_agent_creation(bureau_skill, pillar_yaml, mock_firewall, logger):
    agent = BaseSpecialistAgent(bureau_skill, pillar_yaml, mock_firewall, logger)
    assert agent.skill.name == "bureau"
    assert agent.rolling_summary == ""


def test_build_system_prompt(bureau_skill, pillar_yaml, mock_firewall, logger):
    agent = BaseSpecialistAgent(bureau_skill, pillar_yaml, mock_firewall, logger)
    prompt = agent._build_system_prompt()
    assert "bureau" in prompt
    assert "Delinquency Risk" in prompt
    assert "Flag 90D+" in prompt


def test_build_system_prompt_includes_rolling_summary(
    bureau_skill, pillar_yaml, mock_firewall, logger
):
    agent = BaseSpecialistAgent(bureau_skill, pillar_yaml, mock_firewall, logger)
    agent.rolling_summary = "Q: test?\nA: yes\n---\n"
    prompt = agent._build_system_prompt()
    assert "Q: test?" in prompt
    assert "A: yes" in prompt


def test_update_rolling_summary(bureau_skill, pillar_yaml, mock_firewall, logger):
    agent = BaseSpecialistAgent(bureau_skill, pillar_yaml, mock_firewall, logger)
    agent._update_rolling_summary("What is X?", "X is 42")
    assert "Q: What is X?" in agent.rolling_summary
    assert "A: X is 42" in agent.rolling_summary


def test_rolling_summary_truncation(bureau_skill, pillar_yaml, mock_firewall, logger):
    agent = BaseSpecialistAgent(bureau_skill, pillar_yaml, mock_firewall, logger)
    for i in range(50):
        agent._update_rolling_summary(f"Question {i}?", "A" * 200)
    assert len(agent.rolling_summary) < 5000


def test_run_returns_specialist_output(bureau_skill, pillar_yaml, mock_firewall, logger):
    # Mock 3 sequential LLM results
    mock_firewall.call = MagicMock(
        side_effect=[
            LLMResult(status="success", data={"tables": ["bureau_full"]}),
            LLMResult(
                status="success",
                data={"findings": "Score is 580", "evidence": ["bureau_full"]},
            ),
            LLMResult(
                status="success",
                data={
                    "findings": "Bureau score 580 indicates elevated risk",
                    "evidence": ["bureau_full row 1"],
                    "implications": ["higher default probability"],
                    "data_gaps": [],
                },
            ),
        ]
    )

    agent = BaseSpecialistAgent(bureau_skill, pillar_yaml, mock_firewall, logger)
    output = agent.run("What is the credit risk?", mode="chat")

    assert isinstance(output, SpecialistOutput)
    assert output.domain == "bureau"
    assert output.question == "What is the credit risk?"
    assert output.mode == "chat"
    assert "580" in output.findings
    assert agent.questions_answered == 1
