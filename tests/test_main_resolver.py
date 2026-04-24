"""Unit tests for main._resolve_data_source."""
from pathlib import Path

import pytest

from main import _resolve_data_source


def _make_tables(tmp_path: Path, which: list[str]) -> Path:
    """Create a data_tables/ tree with the named subdirs each containing one case."""
    root = tmp_path / "data_tables"
    for name in which:
        case_dir = root / name / "CASE-00001"
        case_dir.mkdir(parents=True)
        (case_dir / "t.csv").write_text("a,b\n1,2\n")
    root.mkdir(exist_ok=True)
    return root


def test_auto_prefers_real(tmp_path):
    root = _make_tables(tmp_path, ["real", "simulated"])
    assert _resolve_data_source("auto", root) == ("real", root / "real")


def test_auto_falls_back_to_simulated(tmp_path):
    root = _make_tables(tmp_path, ["simulated"])
    assert _resolve_data_source("auto", root) == ("simulated", root / "simulated")


def test_auto_falls_back_to_generator(tmp_path):
    root = _make_tables(tmp_path, [])
    assert _resolve_data_source("auto", root) == ("generator", None)


def test_real_flag_errors_when_empty(tmp_path):
    root = _make_tables(tmp_path, [])
    with pytest.raises(SystemExit):
        _resolve_data_source("real", root)


def test_simulated_flag_errors_when_empty(tmp_path):
    root = _make_tables(tmp_path, [])
    with pytest.raises(SystemExit):
        _resolve_data_source("simulated", root)


def test_generator_flag_always_works(tmp_path):
    root = _make_tables(tmp_path, ["real", "simulated"])
    assert _resolve_data_source("generator", root) == ("generator", None)


def test_real_flag_uses_real_when_non_empty(tmp_path):
    root = _make_tables(tmp_path, ["real"])
    assert _resolve_data_source("real", root) == ("real", root / "real")


def test_simulated_flag_uses_simulated_when_non_empty(tmp_path):
    root = _make_tables(tmp_path, ["simulated"])
    assert _resolve_data_source("simulated", root) == ("simulated", root / "simulated")
