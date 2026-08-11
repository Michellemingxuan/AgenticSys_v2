"""Tests for DataCatalog and LocalDataGateway (case-scoped)."""

import pytest

from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway, normalize_case_id


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
    gw = LocalDataGateway(case_data=case_data)
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
    gw = LocalDataGateway.from_generated(tables_raw)
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


def test_gateway_error_uses_neutral_path_token():
    """Error strings surfaced to callers must use <case> token, not the raw case ID."""
    from datalayer.gateway import LocalDataGateway

    gw = LocalDataGateway(case_data={"77165907010": {"payments": [{"amt": 100}]}})
    gw.set_case("77165907010")

    assert gw._display_path("payments") == "<case>/payments.csv"
    assert "77165907010" not in gw._display_path("payments")


# ── CSV padding is trimmed at the load boundary ─────────────────────────────
#
# Real exports pad string columns to fixed width — 8,587 of 8,888 `Merchant
# Name` cells on case 366132845011. Filters already compared stripped, but the
# padding reached OUTPUT: ragged chart labels, padded merchant names quoted in
# findings and KB claims, and two spellings of one merchant that don't compare
# equal as strings.

def test_from_case_folders_trims_padded_cells(tmp_path):
    case = tmp_path / "CASE-1"
    case.mkdir()
    (case / "spends.csv").write_text(
        "Merchant Name ,Amount\n"
        "S BERTRAM     ,100\n"
        "  FRAYLICH,200\n"
    )
    gw = LocalDataGateway.from_case_folders(str(tmp_path))
    gw.set_case("CASE-1")
    rows = gw.query("spends")
    assert [r["Merchant Name"] for r in rows] == ["S BERTRAM", "FRAYLICH"]
    # Padded HEADERS are trimmed too — otherwise column resolution misses.
    assert "Merchant Name" in rows[0]


def test_trimming_makes_one_merchant_compare_equal(tmp_path):
    case = tmp_path / "CASE-2"
    case.mkdir()
    (case / "spends.csv").write_text(
        "Merchant Name,Amount\nACME  ,100\nACME,200\n")
    gw = LocalDataGateway.from_case_folders(str(tmp_path))
    gw.set_case("CASE-2")
    assert len({r["Merchant Name"] for r in gw.query("spends")}) == 1


def test_non_string_and_empty_cells_survive_trimming(tmp_path):
    case = tmp_path / "CASE-3"
    case.mkdir()
    (case / "t.csv").write_text("a,b\n , 5 \n")
    gw = LocalDataGateway.from_case_folders(str(tmp_path))
    gw.set_case("CASE-3")
    row = gw.query("t")[0]
    assert row["a"] == "" and row["b"] == "5"


# ── Case-id normalization at ingress ──────────────────────────────


def test_normalize_case_id_strips_invisible_padding():
    """Whitespace AND the zero-width family, which `str.strip()` leaves.

    The padding characters are built with `chr()` rather than pasted in as
    literals: they render as nothing, so a literal would make this test's
    intent invisible in review — and a stray edit could delete one without
    a trace.
    """
    nbsp, zwsp, bom = chr(0x00A0), chr(0x200B), chr(0xFEFF)
    assert normalize_case_id("11854808010 ") == "11854808010"
    assert normalize_case_id("  11854808010\t\n") == "11854808010"
    assert normalize_case_id(nbsp + "11854808010") == "11854808010"
    assert normalize_case_id(zwsp + "11854808010" + zwsp) == "11854808010"
    assert normalize_case_id(bom + "11854808010 ") == "11854808010"


def test_normalize_case_id_preserves_every_visible_character():
    """Ids differ in LENGTH and carry no fixed width or check digit, so the
    normalizer must never pad, truncate, or reject on length — only strip
    invisible characters. Interior characters are untouched."""
    assert normalize_case_id("11854808010") == "11854808010"   # 11 chars
    assert normalize_case_id("366132845011") == "366132845011"  # 12 chars
    assert normalize_case_id("7") == "7"
    assert normalize_case_id("CASE-abc_01") == "CASE-abc_01"
    # An interior space is a real (if odd) part of the id — not padding.
    assert normalize_case_id(" a b ") == "a b"


def test_normalize_case_id_coerces_non_string_input():
    """A CSV parser can hand back an int id; callers get one type back."""
    assert normalize_case_id(366132845011) == "366132845011"
    assert normalize_case_id(None) == ""


def test_case_folder_with_trailing_space_loads_under_the_clean_id(tmp_path):
    """Regression: `data_tables/real/` holds a folder named "11854808010 ".
    The padded name became the case id, which then built `reports/<id>/` and
    the log filename — forking one case across two directories."""
    case = tmp_path / "11854808010 "
    case.mkdir()
    (case / "spends.csv").write_text("Amount\n100\n")

    gw = LocalDataGateway.from_case_folders(str(tmp_path))

    assert gw.list_case_ids() == ["11854808010"]      # no trailing space
    # Both spellings resolve — `set_case` normalizes, so an old bookmark or
    # a stale session key still finds the loaded case.
    gw.set_case("11854808010 ")
    assert gw.get_case_id() == "11854808010"
    assert gw.query("spends") == [{"Amount": "100"}]
    gw.set_case("11854808010")
    assert gw.query("spends") == [{"Amount": "100"}]


def test_two_folder_spellings_of_one_case_merge_instead_of_clobbering(tmp_path):
    """Padded and clean spellings of the same id land in ONE case. The later
    folder must not erase the earlier one's tables (the `= {}` bug)."""
    padded = tmp_path / "CASE-9 "
    padded.mkdir()
    (padded / "spends.csv").write_text("Amount\n100\n")
    clean = tmp_path / "CASE-9"
    clean.mkdir()
    (clean / "payments_success.csv").write_text("Payment Amount\n50\n")

    gw = LocalDataGateway.from_case_folders(str(tmp_path))

    assert gw.list_case_ids() == ["CASE-9"]
    gw.set_case("CASE-9")
    # Both folders' tables are present — neither wiped the other.
    assert set(gw.list_tables()) >= {"spends"}
    assert gw.query("spends") == [{"Amount": "100"}]


def test_duplicate_table_across_spellings_keeps_first_and_warns(tmp_path, capsys):
    """When both spellings define the SAME table, rows are not concatenated
    (that would double-count) and the skipped folder is named on stdout."""
    padded = tmp_path / "CASE-8 "
    padded.mkdir()
    (padded / "spends.csv").write_text("Amount\n100\n")
    clean = tmp_path / "CASE-8"
    clean.mkdir()
    (clean / "spends.csv").write_text("Amount\n999\n")

    gw = LocalDataGateway.from_case_folders(str(tmp_path))
    gw.set_case("CASE-8")

    rows = gw.query("spends")
    assert len(rows) == 1                       # not concatenated
    out = capsys.readouterr().out
    assert "spends" in out and "CASE-8" in out  # the conflict is announced


def test_from_generated_normalizes_case_ids():
    """The long-format CSV door normalizes too — a padded `case_id` COLUMN
    must not create a second case alongside the clean one."""
    tables_raw = {
        "spends": {
            "case_id": ["CASE-7 ", "CASE-7", " CASE-7"],
            "Amount": [1, 2, 3],
        }
    }
    gw = LocalDataGateway.from_generated(tables_raw)
    assert gw.list_case_ids() == ["CASE-7"]
    gw.set_case("CASE-7")
    assert len(gw.query("spends")) == 3
