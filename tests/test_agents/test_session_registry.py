"""Tests for agents.session_registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.base_agent import BaseSpecialistAgent
from agents.session_registry import SessionRegistry
from gateway.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import DomainSkill


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test", log_dir=str(tmp_path))


@pytest.fixture
def bureau_skill():
    return DomainSkill(
        name="bureau",
        system_prompt="Bureau analyst",
        data_hints=["bureau_full"],
    )


@pytest.fixture
def pillar_yaml():
    return {"focus": "credit_risk"}


@pytest.fixture
def firewall(logger):
    adapter = MagicMock()
    return FirewallStack(adapter, logger)


def test_create_new_specialist(bureau_skill, pillar_yaml, firewall, logger):
    reg = SessionRegistry()
    agent = reg.get_or_create("bureau", "credit_risk", bureau_skill, pillar_yaml, firewall, logger)
    assert isinstance(agent, BaseSpecialistAgent)
    assert agent.skill.name == "bureau"


def test_reuse_existing(bureau_skill, pillar_yaml, firewall, logger):
    reg = SessionRegistry()
    a1 = reg.get_or_create("bureau", "credit_risk", bureau_skill, pillar_yaml, firewall, logger)
    a1.rolling_summary = "some summary"
    a2 = reg.get_or_create("bureau", "credit_risk", bureau_skill, pillar_yaml, firewall, logger)
    assert a1 is a2
    assert a2.rolling_summary == "some summary"


def test_different_pillar_creates_new(bureau_skill, pillar_yaml, firewall, logger):
    reg = SessionRegistry()
    a1 = reg.get_or_create("bureau", "credit_risk", bureau_skill, pillar_yaml, firewall, logger)
    a2 = reg.get_or_create("bureau", "cbo", bureau_skill, {"focus": "cbo"}, firewall, logger)
    assert a1 is not a2


def test_list_active(bureau_skill, pillar_yaml, firewall, logger):
    reg = SessionRegistry()
    reg.get_or_create("bureau", "credit_risk", bureau_skill, pillar_yaml, firewall, logger)
    active = reg.list_active()
    assert len(active) == 1
    assert active[0]["domain"] == "bureau"
    assert active[0]["pillar"] == "credit_risk"
    assert active[0]["questions_answered"] == 0
    assert "summary_preview" in active[0]


def test_clear(bureau_skill, pillar_yaml, firewall, logger):
    reg = SessionRegistry()
    reg.get_or_create("bureau", "credit_risk", bureau_skill, pillar_yaml, firewall, logger)
    assert len(reg.list_active()) == 1
    reg.clear()
    assert len(reg.list_active()) == 0
