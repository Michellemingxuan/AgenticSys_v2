"""Tests for gateway.llm_factory.build_llm."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gateway.firewall_stack import FirewallStack, FirewalledModel
from gateway.llm_factory import build_llm
from logger.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-factory", log_dir=str(tmp_path))


def test_build_llm_returns_firewalled_model(logger):
    firewall = FirewallStack(logger=logger)

    with patch("gateway.llm_factory.ChatOpenAI") as mock_chat:
        mock_chat.return_value = object()  # opaque LangChain model stand-in
        llm = build_llm("gpt-4.1", firewall)

    assert isinstance(llm, FirewalledModel)
    assert llm.firewall is firewall
    mock_chat.assert_called_once_with(model="gpt-4.1")
