"""Verify Orchestrator builds + uses case_schema so unfiltered catalog
entries do not leak into team-construction prompts."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.session_registry import SessionRegistry
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from orchestrator.orchestrator import Orchestrator


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-orch-cf", log_dir=str(tmp_path))


def _orch(catalog, gateway, logger):
    return Orchestrator(
        llm=AsyncMock(),
        logger=logger,
        registry=SessionRegistry(),
        pillar="credit_risk",
        catalog=catalog,
        gateway=gateway,
    )


def test_build_case_schema_from_active_case(logger):
    gateway = LocalDataGateway(case_data={
        "case_X": {"transactions": [{"amount": 1, "date": "2025-01-01"}]},
    })
    gateway.set_case("case_X")
    orch = _orch(catalog=None, gateway=gateway, logger=logger)
    assert orch._build_case_schema() == {"transactions": ["amount", "date"]}


def test_build_case_schema_returns_none_without_gateway(logger):
    orch = _orch(catalog=None, gateway=None, logger=logger)
    assert orch._build_case_schema() is None


def test_case_aware_columns_resolves_via_substring(logger, tmp_path):
    """Real ``bureau_data`` table maps to canonical ``bureau`` (substring match)."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "bureau.yaml").write_text("""\
table: bureau
description: "bureau"
columns:
  fico_score:
    dtype: int
    description: "FICO"
""")
    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway(case_data={
        "case_X": {"bureau_data": [{"FICO Score": 720, "month": "2024-01"}]},
    })
    gateway.set_case("case_X")
    orch = _orch(catalog=catalog, gateway=gateway, logger=logger)
    case_schema = orch._build_case_schema()
    assert orch._case_aware_columns("bureau", case_schema) == ["FICO Score", "month"]


def test_case_aware_columns_returns_none_for_absent_table(logger, tmp_path):
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "cust_tenure.yaml").write_text("""\
table: cust_tenure
description: "tenure"
columns:
  tenure_months:
    dtype: int
    description: "tenure"
""")
    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway(case_data={
        "case_X": {"only_one_table": [{"x": 1}]},
    })
    gateway.set_case("case_X")
    orch = _orch(catalog=catalog, gateway=gateway, logger=logger)
    case_schema = orch._build_case_schema()
    # cust_tenure has no real-table counterpart → not present
    assert orch._case_aware_columns("cust_tenure", case_schema) is None
