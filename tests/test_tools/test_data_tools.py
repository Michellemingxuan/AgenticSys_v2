"""Tests for tools/data_tools.py (case-scoped)."""

import pytest

from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from tools import data_tools


@pytest.fixture(autouse=True)
def _setup_tools():
    case_data = {
        "CASE-00001": {
            "bureau_full": [
                {"score": 720, "derog_count": 0},
                {"score": 680, "derog_count": 1},
            ],
        },
        "CASE-00002": {
            "bureau_full": [
                {"score": 580, "derog_count": 4},
            ],
        },
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    gateway.set_case("CASE-00001")
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)
    yield
    data_tools._gateway = None
    data_tools._catalog = None


def test_list_tables():
    result = data_tools.list_available_tables()
    assert "bureau_full" in result
    assert "Tables for the current case:" in result
    # No raw case ID must leak.
    import re
    assert re.search(r"CASE-\d+", result) is None


def test_list_tables_no_case_set():
    """When no case is active, the tool must fall back to catalog-level listing
    instead of mislabeling all-case tables as 'current case'."""
    case_data = {
        "CASE-00001": {"bureau_full": [{"score": 720}]},
        "CASE-00002": {"bureau_full": [{"score": 580}]},
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    # Intentionally do NOT call gateway.set_case(...)
    catalog = DataCatalog(profile_dir="config/data_profiles")
    data_tools.init_tools(gateway, catalog)

    result = data_tools.list_available_tables()

    # The "current case" header must NOT appear when no case is set.
    assert "Tables for the current case:" not in result
    # And absolutely no raw case ID leaks.
    import re
    assert re.search(r"CASE-\d+", result) is None
    # The catalog fallback should list at least one real table.
    assert "bureau" in result

    # Cleanup
    data_tools._gateway = None
    data_tools._catalog = None


def test_get_schema():
    result = data_tools.get_table_schema("bureau")
    assert "type" in result
    # case_id is infrastructure, not schema — must not appear in LLM-bound schema output.
    assert "case_id" not in result
    assert "CASE-" not in result


def test_get_schema_missing():
    result = data_tools.get_table_schema("nonexistent")
    assert result == "Data unavailable"


def test_query_all():
    result = data_tools.query_table("bureau_full")
    assert "720" in result
    assert "680" in result


def test_query_filtered():
    result = data_tools.query_table("bureau_full", filter_column="score", filter_value=720)
    assert "720" in result


def test_query_missing():
    result = data_tools.query_table("no_such_table")
    assert "unavailable" in result.lower()
