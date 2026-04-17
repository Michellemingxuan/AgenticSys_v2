"""Tests for tools/data_tools.py."""

import pytest

from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from tools import data_tools


SAMPLE_ROWS = [
    {"case_id": "CASE-00001", "score": 720, "derog_count": 0},
    {"case_id": "CASE-00002", "score": 580, "derog_count": 4},
]


@pytest.fixture(autouse=True)
def _setup_tools():
    catalog = DataCatalog(profile_dir="config/data_profiles")
    gateway = SimulatedDataGateway({"bureau_full": SAMPLE_ROWS})
    data_tools.init_tools(gateway, catalog)
    yield
    # reset
    data_tools._gateway = None
    data_tools._catalog = None


def test_list_tables():
    result = data_tools.list_available_tables()
    assert "bureau_full" in result


def test_get_schema():
    result = data_tools.get_table_schema("bureau_full")
    assert "case_id" in result
    assert "type" in result


def test_get_schema_missing():
    result = data_tools.get_table_schema("nonexistent")
    assert result == "Data unavailable"


def test_query_all():
    result = data_tools.query_table("bureau_full")
    assert "CASE-00001" in result
    assert "CASE-00002" in result


def test_query_filtered():
    result = data_tools.query_table("bureau_full", filter_column="case_id", filter_value="CASE-00001")
    assert "CASE-00001" in result
    assert "CASE-00002" not in result


def test_query_missing():
    result = data_tools.query_table("no_such_table")
    assert result == "Data unavailable"
