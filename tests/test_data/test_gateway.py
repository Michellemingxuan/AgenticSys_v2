"""Tests for DataCatalog and SimulatedDataGateway."""

import pytest

from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway


# ── Catalog fixtures ──────────────────────────────────────────────

@pytest.fixture()
def catalog():
    return DataCatalog(profile_dir="config/data_profiles")


# ── Gateway fixtures ──────────────────────────────────────────────

SAMPLE_ROWS = [
    {"case_id": "CASE-00001", "score": 720, "derog_count": 0},
    {"case_id": "CASE-00002", "score": 580, "derog_count": 4},
    {"case_id": "CASE-00003", "score": 650, "derog_count": 1},
]

@pytest.fixture()
def gateway():
    tables = {"bureau_full": SAMPLE_ROWS}
    return SimulatedDataGateway(tables)


# ── Catalog tests ─────────────────────────────────────────────────

def test_catalog_lists_tables(catalog):
    tables = catalog.list_tables()
    assert isinstance(tables, list)
    assert len(tables) > 0
    assert "bureau_full" in tables


def test_catalog_get_schema(catalog):
    schema = catalog.get_schema("bureau_full")
    assert schema is not None
    assert "case_id" in schema
    assert "type" in schema["case_id"]


def test_catalog_get_schema_missing(catalog):
    assert catalog.get_schema("nonexistent_table") is None


# ── Gateway tests ─────────────────────────────────────────────────

def test_gateway_query_all(gateway):
    rows = gateway.query("bureau_full")
    assert rows is not None
    assert len(rows) == 3


def test_gateway_query_with_filter(gateway):
    rows = gateway.query("bureau_full", filters={"case_id": "CASE-00001"})
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["score"] == 720


def test_gateway_query_missing_table(gateway):
    assert gateway.query("no_such_table") is None


def test_gateway_query_multi_row(gateway):
    rows = gateway.query("bureau_full", limit=2)
    assert rows is not None
    assert len(rows) == 2
