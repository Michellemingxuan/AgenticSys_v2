"""Tests for DataCatalog and SimulatedDataGateway (case-scoped)."""

import pytest

from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway


# ── Catalog fixtures ──────────────────────────────────────────────

@pytest.fixture()
def catalog():
    return DataCatalog(profile_dir="config/data_profiles")


# ── Gateway fixtures ──────────────────────────────────────────────

@pytest.fixture()
def gateway():
    """Per-case data: each case has its own set of tables."""
    case_data = {
        "CASE-00001": {
            "bureau_full": [
                {"score": 720, "derog_count": 0},
            ],
            "pmts_detail": [
                {"status": "on_time", "amount": 500},
                {"status": "late", "amount": 200},
            ],
        },
        "CASE-00002": {
            "bureau_full": [
                {"score": 580, "derog_count": 4},
            ],
            "pmts_detail": [
                {"status": "missed", "amount": 0},
            ],
        },
    }
    gw = SimulatedDataGateway(case_data=case_data)
    return gw


# ── Catalog tests ─────────────────────────────────────────────────

def test_catalog_lists_tables(catalog):
    tables = catalog.list_tables()
    assert isinstance(tables, list)
    assert len(tables) > 0
    assert "bureau" in tables


def test_catalog_get_schema(catalog):
    schema = catalog.get_schema("bureau")
    assert schema is not None
    # case_id is generator infrastructure, not table schema — must not surface here.
    assert "case_id" not in schema
    # Spot-check that a real bureau column IS present with its metadata.
    assert "fico_score" in schema
    assert "type" in schema["fico_score"]


def test_catalog_get_schema_missing(catalog):
    assert catalog.get_schema("nonexistent_table") is None


# ── Gateway tests — case-scoped ──────────────────────────────────

def test_gateway_set_case(gateway):
    gateway.set_case("CASE-00001")
    assert gateway.get_case_id() == "CASE-00001"


def test_gateway_list_case_ids(gateway):
    cases = gateway.list_case_ids()
    assert "CASE-00001" in cases
    assert "CASE-00002" in cases


def test_gateway_query_scoped_to_case(gateway):
    gateway.set_case("CASE-00001")
    rows = gateway.query("bureau_full")
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["score"] == 720


def test_gateway_query_different_case(gateway):
    gateway.set_case("CASE-00002")
    rows = gateway.query("bureau_full")
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["score"] == 580


def test_gateway_query_multi_row_table(gateway):
    gateway.set_case("CASE-00001")
    rows = gateway.query("pmts_detail")
    assert rows is not None
    assert len(rows) == 2


def test_gateway_query_with_filter(gateway):
    gateway.set_case("CASE-00001")
    rows = gateway.query("pmts_detail", filters={"status": "late"})
    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["amount"] == 200


def test_gateway_query_missing_table(gateway):
    gateway.set_case("CASE-00001")
    assert gateway.query("no_such_table") is None


def test_gateway_query_without_case_set(gateway):
    """Query without setting a case returns None."""
    assert gateway.query("bureau_full") is None


def test_gateway_list_tables_for_case(gateway):
    gateway.set_case("CASE-00001")
    tables = gateway.list_tables()
    assert "bureau_full" in tables
    assert "pmts_detail" in tables


def test_gateway_from_generated():
    """Test building per-case gateway from generator output."""
    tables_raw = {
        "bureau_full": {
            "case_id": ["CASE-00001", "CASE-00002"],
            "score": [720, 580],
            "derog_count": [0, 4],
        },
        "pmts_detail": {
            "case_id": ["CASE-00001", "CASE-00001", "CASE-00002"],
            "status": ["on_time", "late", "missed"],
            "amount": [500, 200, 0],
        },
    }
    gw = SimulatedDataGateway.from_generated(tables_raw)
    cases = gw.list_case_ids()
    assert "CASE-00001" in cases
    assert "CASE-00002" in cases

    gw.set_case("CASE-00001")
    rows = gw.query("bureau_full")
    assert len(rows) == 1
    assert rows[0]["score"] == 720
    # case_id should NOT be in the row data (it's implicit from case context)
    assert "case_id" not in rows[0]

    pmts = gw.query("pmts_detail")
    assert len(pmts) == 2
