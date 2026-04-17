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
        assert len(gen.profiles) == 9

    def test_profile_names(self, gen: DataGenerator):
        expected = {
            "bureau_full", "bureau_trades", "txn_monthly", "pmts_detail",
            "model_scores", "wcc_flags", "xbu_summary", "cust_tenure", "income_dti",
        }
        assert set(gen.profiles.keys()) == expected


class TestGeneration:
    def test_correct_row_count(self, gen: DataGenerator):
        tables = gen.generate_all()
        for name, cols in tables.items():
            expected = gen.profiles[name]["row_count"]
            first_col = next(iter(cols.values()))
            assert len(first_col) == expected, f"{name} row count mismatch"

    def test_respects_int_range(self, gen: DataGenerator):
        tables = gen.generate_all()
        # bureau_full.score should be in [300, 850]
        scores = tables["bureau_full"]["score"]
        assert all(300 <= s <= 850 for s in scores), "score out of range"

    def test_categorical_values(self, gen: DataGenerator):
        tables = gen.generate_all()
        trade_types = set(tables["bureau_trades"]["trade_type"])
        valid = {"revolving", "installment", "mortgage", "other"}
        assert trade_types.issubset(valid), f"Unexpected trade_type values: {trade_types - valid}"

    def test_sequential_case_ids(self, gen: DataGenerator):
        tables = gen.generate_all()
        ids = tables["bureau_full"]["case_id"]
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
            assert len(paths) == 9
            for p in paths:
                assert os.path.exists(p)
                # Check file has header + data rows
                with open(p) as f:
                    lines = f.readlines()
                assert len(lines) > 1, f"CSV {p} has no data rows"
