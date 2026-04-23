"""Tests for FirewalledModel — the LangChain-backed firewall wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.firewall_stack import FirewallStack, FirewalledModel
from logger.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-fwm", log_dir=str(tmp_path))


@pytest.fixture
def firewall(logger):
    return FirewallStack(logger=logger)


@pytest.fixture
def fake_model():
    """A LangChain-shaped chat model whose .ainvoke is mockable."""
    model = MagicMock()
    model.ainvoke = AsyncMock()
    model.bind_tools = MagicMock(return_value=model)  # bind_tools is sync, returns a model
    return model


def test_firewalled_model_construction(firewall, fake_model):
    fwm = firewall.wrap(fake_model)
    assert isinstance(fwm, FirewalledModel)
    assert fwm.firewall is firewall
    assert fwm.model is fake_model
