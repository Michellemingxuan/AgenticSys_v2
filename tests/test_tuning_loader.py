"""config/tuning.yaml -> os.environ, with inline-env-wins (setdefault) semantics."""
import os

import pytest

from config.tuning_loader import apply_tuning

_KEYS = ["EPISODIC_TURNS", "AMEM_CONSOLIDATE_EVERY_N", "AMEM_ACTIVE_KP_THRESHOLD"]


@pytest.fixture
def clean_env():
    saved = {k: os.environ.get(k) for k in _KEYS}
    for k in _KEYS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_apply_tuning_sets_env_from_yaml(tmp_path, clean_env):
    y = tmp_path / "t.yaml"
    y.write_text("memory:\n  episodic_turns: 4\n  active_kp_threshold: 7\n")
    applied = apply_tuning(str(y))
    assert os.environ["EPISODIC_TURNS"] == "4"
    assert os.environ["AMEM_ACTIVE_KP_THRESHOLD"] == "7"
    assert applied == {"EPISODIC_TURNS": "4", "AMEM_ACTIVE_KP_THRESHOLD": "7"}


def test_inline_env_wins_over_yaml(tmp_path, clean_env):
    y = tmp_path / "t.yaml"
    y.write_text("memory:\n  episodic_turns: 4\n")
    os.environ["EPISODIC_TURNS"] = "99"           # as if set inline / in .env
    applied = apply_tuning(str(y))
    assert os.environ["EPISODIC_TURNS"] == "99"    # not overridden
    assert "EPISODIC_TURNS" not in applied


def test_missing_or_bad_file_is_noop(tmp_path, clean_env):
    assert apply_tuning(str(tmp_path / "nope.yaml")) == {}
    assert "EPISODIC_TURNS" not in os.environ


def test_active_kp_threshold_reads_env(monkeypatch):
    # The loader module reads AMEM_ACTIVE_KP_THRESHOLD at import; reload to prove
    # the wiring, then reload back so other modules' bound value is unaffected.
    import importlib
    import memory.loader as loader
    monkeypatch.setenv("AMEM_ACTIVE_KP_THRESHOLD", "3")
    importlib.reload(loader)
    assert loader.ACTIVE_KP_THRESHOLD == 3
    monkeypatch.delenv("AMEM_ACTIVE_KP_THRESHOLD", raising=False)
    importlib.reload(loader)
    assert loader.ACTIVE_KP_THRESHOLD == 100
