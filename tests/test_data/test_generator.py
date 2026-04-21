"""Tests for data.generator.DataGenerator."""

from __future__ import annotations

import os
import tempfile

import pytest

from data.generator import DataGenerator

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config", "data_profiles")


@pytest.fixture
def gen():
    g = DataGenerator(profile_dir=PROFILE_DIR, seed=42)
    g.load_profiles()
    return g


class TestLoadProfiles:
    def test_loads_all_profiles(self, gen: DataGenerator):
        assert len(gen.profiles) == 11

    def test_profile_names(self, gen: DataGenerator):
        expected = {
            "bureau", "txn_monthly", "spends", "payments",
            "model_scores", "score_drivers", "wcc_flags", "xbu_summary",
            "cust_tenure", "income_dti", "cross_bu",
        }
        assert set(gen.profiles.keys()) == expected


class TestGeneration:
    def test_correct_row_count(self, gen: DataGenerator):
        tables = gen.generate_all()
        case_count = gen._get_case_count()
        for name, cols in tables.items():
            profile = gen.profiles[name]
            if "rows_per_case" in profile:
                expected = profile["rows_per_case"] * case_count
            elif profile.get("one_row_per_case", False):
                expected = case_count
            else:
                expected = profile["row_count"]
            first_col = next(iter(cols.values()))
            assert len(first_col) == expected, f"{name} row count mismatch: got {len(first_col)}, expected {expected}"

    def test_respects_int_range(self, gen: DataGenerator):
        tables = gen.generate_all()
        # bureau.fico_score should be in [300, 850]
        scores = tables["bureau"]["fico_score"]
        assert all(300 <= s <= 850 for s in scores), "fico_score out of range"

    def test_categorical_values(self, gen: DataGenerator):
        tables = gen.generate_all()
        trade_types = set(tables["bureau"]["trade_type"])
        valid = {"revolving", "installment", "mortgage", "commercial", "other"}
        assert trade_types.issubset(valid), f"Unexpected trade_type values: {trade_types - valid}"

    def test_sequential_case_ids(self, gen: DataGenerator):
        tables = gen.generate_all()
        ids = tables["bureau"]["case_id"]
        assert ids[0] == "CASE-00001"
        assert ids[1] == "CASE-00002"
        assert ids[-1] == "CASE-00050"

    def test_deterministic_with_seed(self):
        g1 = DataGenerator(profile_dir=PROFILE_DIR, seed=99)
        g1.load_profiles()
        t1 = g1.generate_all()

        g2 = DataGenerator(profile_dir=PROFILE_DIR, seed=99)
        g2.load_profiles()
        t2 = g2.generate_all()

        for name in t1:
            for col in t1[name]:
                assert t1[name][col] == t2[name][col], f"Non-deterministic: {name}.{col}"

    def test_row_count_override(self, gen: DataGenerator):
        tables = gen.generate_all(row_count_override=10)
        for name, cols in tables.items():
            first_col = next(iter(cols.values()))
            assert len(first_col) == 10, f"{name} override failed"


class TestDumpCSV:
    def test_dumps_csv(self, gen: DataGenerator):
        gen.generate_all()
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = gen.dump_csv(tmpdir)
            assert len(paths) == 11
            for p in paths:
                assert os.path.exists(p)
                # Check file has header + data rows
                with open(p) as f:
                    lines = f.readlines()
                assert len(lines) > 1, f"CSV {p} has no data rows"


def test_generator_injects_case_id_column(tmp_path):
    """Generator adds a case_id column to every table, even when the YAML profile does not declare it."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "noop.yaml").write_text(
        "table: noop\n"
        "description: minimal fixture with no case_id column\n"
        "one_row_per_case: true\n"
        "columns:\n"
        "  value:\n"
        "    dtype: int\n"
        "    distribution: uniform\n"
        "    min: 0\n"
        "    max: 10\n"
        "    description: placeholder\n"
    )

    from data.generator import DataGenerator, CASE_ID_COLUMN, CASE_ID_FORMAT
    gen = DataGenerator(profile_dir=str(profile_dir), seed=1, cases=3)
    gen.load_profiles()
    tables = gen.generate_all()

    cols = tables["noop"]
    assert CASE_ID_COLUMN in cols
    # 3 cases, one_row_per_case → 3 rows with CASE-00001..CASE-00003
    assert cols[CASE_ID_COLUMN] == [
        CASE_ID_FORMAT.format(seq=1),
        CASE_ID_FORMAT.format(seq=2),
        CASE_ID_FORMAT.format(seq=3),
    ]


def test_generator_preserves_profile_declared_case_id(tmp_path):
    """If the profile already declares case_id, the generator must leave it untouched (idempotency)."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "custom.yaml").write_text(
        "table: custom\n"
        "description: profile with its own case_id declaration\n"
        "one_row_per_case: true\n"
        "columns:\n"
        "  case_id:\n"
        "    dtype: string\n"
        "    format: \"CUSTOM-{seq:04d}\"\n"
        "    description: custom case id format\n"
        "  value:\n"
        "    dtype: int\n"
        "    distribution: uniform\n"
        "    min: 0\n"
        "    max: 10\n"
        "    description: placeholder\n"
    )

    from data.generator import DataGenerator
    gen = DataGenerator(profile_dir=str(profile_dir), seed=1, cases=3)
    gen.load_profiles()
    tables = gen.generate_all()

    cols = tables["custom"]
    # Generator must not overwrite the profile-declared case_id.
    assert cols["case_id"] == ["CUSTOM-0001", "CUSTOM-0002", "CUSTOM-0003"]


def test_generator_full_suite_no_profile_case_id():
    """All 11 real profiles generate correctly with case_id removed from YAMLs.

    After Task 2, no profile declares case_id. The generator should still produce
    a case_id column in every table (via the infrastructure injection from Task 1).
    """
    from data.generator import DataGenerator, CASE_ID_COLUMN

    gen = DataGenerator(profile_dir=PROFILE_DIR, seed=42, cases=5)
    gen.load_profiles()

    # Sanity: no profile declares case_id anymore
    for table_name, profile in gen.profiles.items():
        assert CASE_ID_COLUMN not in profile["columns"], (
            f"{table_name}.yaml still declares case_id — remove it"
        )

    tables = gen.generate_all()
    # Every generated table has case_id with the right format
    import re
    pattern = re.compile(r"^CASE-\d{5}$")
    for table_name, cols in tables.items():
        assert CASE_ID_COLUMN in cols, f"{table_name} missing case_id column"
        for v in cols[CASE_ID_COLUMN]:
            assert pattern.match(v), f"{table_name} has bad case_id: {v!r}"
