"""Unit tests for _load_default_n_cases() in data/__main__.py."""

import data.__main__ as main_mod


def test_load_default_n_cases_happy_path(tmp_path, monkeypatch):
    p = tmp_path / "gen.yaml"
    p.write_text("n_cases: 7\n")
    monkeypatch.setattr(main_mod, "_GENERATION_CONFIG", p)
    assert main_mod._load_default_n_cases() == 7


def test_load_default_n_cases_missing_file(tmp_path, monkeypatch, capsys):
    p = tmp_path / "missing.yaml"
    monkeypatch.setattr(main_mod, "_GENERATION_CONFIG", p)
    assert main_mod._load_default_n_cases() == main_mod._FALLBACK_N_CASES
    assert "WARNING" in capsys.readouterr().out


def test_load_default_n_cases_empty_file(tmp_path, monkeypatch, capsys):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    monkeypatch.setattr(main_mod, "_GENERATION_CONFIG", p)
    assert main_mod._load_default_n_cases() == main_mod._FALLBACK_N_CASES
    assert "WARNING" in capsys.readouterr().out


def test_load_default_n_cases_missing_key(tmp_path, monkeypatch, capsys):
    p = tmp_path / "no_key.yaml"
    p.write_text("other_field: 1\n")
    monkeypatch.setattr(main_mod, "_GENERATION_CONFIG", p)
    assert main_mod._load_default_n_cases() == main_mod._FALLBACK_N_CASES
    assert "WARNING" in capsys.readouterr().out
