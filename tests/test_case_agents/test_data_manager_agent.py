"""Tests for agents.data_manager_agent.DataManagerAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from case_agents.data_manager_agent import DataManagerAgent
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from datalayer.generator import DataGenerator
from logger.event_logger import EventLogger
from tools.data_tools import init_tools


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-dm", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def live_gateway_catalog():
    """Real in-memory gateway + catalog for the current seed.

    Cheaper than stubbing the gateway — the generator runs in milliseconds
    and gives us realistic table/column layouts for the redaction checks.
    """
    gen = DataGenerator(seed=42)
    gen.load_profiles()
    tables_raw = gen.generate_all()
    gateway = LocalDataGateway.from_generated(tables_raw)
    case_ids = gateway.list_case_ids()
    gateway.set_case(case_ids[0])
    catalog = DataCatalog()
    init_tools(gateway, catalog)
    return gateway, catalog


def test_query_happy_path_returns_string(mock_llm, logger, live_gateway_catalog):
    gateway, catalog = live_gateway_catalog
    dm = DataManagerAgent(gateway, catalog, mock_llm, logger)

    result = dm.query("bureau_full")

    assert isinstance(result, str)
    assert len(result) > 0
    # Real gateway returns either rows or a "Data unavailable" / similar note.
    # Either way, the result is non-empty and does not crash.


def test_query_redacts_long_digit_runs(mock_llm, logger, live_gateway_catalog):
    """Any 6+-digit run in the query output is masked before return."""
    gateway, catalog = live_gateway_catalog
    dm = DataManagerAgent(gateway, catalog, mock_llm, logger)

    # Stage a payload with a known digit run via the internal redact helper.
    # This tests the redact path directly, regardless of what the gateway
    # actually emits for this case (which may or may not contain 6+-digit
    # runs depending on seed/case).
    payload = "account 4532123456789 last payment on 2024-09-24"
    redacted = dm._redact(payload)

    assert "4532123456789" not in redacted
    assert "***MASKED***" in redacted
    # Short digit runs (e.g., year 2024, day 24) must survive.
    assert "2024-09-24" in redacted


def test_query_redacts_case_ids(mock_llm, logger, live_gateway_catalog):
    gateway, catalog = live_gateway_catalog
    dm = DataManagerAgent(gateway, catalog, mock_llm, logger)

    payload = "Row from CASE-00042: value=123"
    redacted = dm._redact(payload)

    assert "CASE-00042" not in redacted


def test_describe_catalog_returns_non_empty_prompt(mock_llm, logger, live_gateway_catalog):
    """describe_catalog() fronts the catalog's prompt-context with the data_catalog.md body."""
    gateway, catalog = live_gateway_catalog
    dm = DataManagerAgent(gateway, catalog, mock_llm, logger)

    out = dm.describe_catalog()

    assert isinstance(out, str)
    assert len(out) > 0
    # Skill body markers appear at the top.
    assert "Data Catalog" in out or "catalog surface" in out.lower() or "catalog" in out.lower()


def test_describe_catalog_handles_missing_catalog(mock_llm, logger):
    """When catalog is None, describe_catalog still returns the skill body."""
    dm = DataManagerAgent(gateway=MagicMock(), catalog=None, llm=mock_llm, logger=logger)

    out = dm.describe_catalog()

    assert isinstance(out, str)
    # Skill body is present; the appended catalog context is empty.
    assert len(out) > 0
