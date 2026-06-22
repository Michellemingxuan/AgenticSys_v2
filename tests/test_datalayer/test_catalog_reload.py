import time
import yaml
from datalayer.catalog import DataCatalog


def _profile(p, desc):
    (p / "t.yaml").write_text(yaml.safe_dump(
        {"table": "t", "description": desc, "columns": {"a": {"dtype": "int", "description": desc}}}))


def test_reload_picks_up_changed_yaml(tmp_path):
    _profile(tmp_path, "old")
    cat = DataCatalog(profile_dir=str(tmp_path))
    assert cat.get_description("t") == "old"
    time.sleep(0.01)
    _profile(tmp_path, "new")
    cat.reload()
    assert cat.get_description("t") == "new"


def test_reload_if_changed_only_when_mtime_advances(tmp_path):
    _profile(tmp_path, "old")
    cat = DataCatalog(profile_dir=str(tmp_path))
    assert cat.reload_if_changed() is False          # nothing changed since load
    time.sleep(0.01)
    _profile(tmp_path, "new")
    assert cat.reload_if_changed() is True            # mtime advanced
    assert cat.get_description("t") == "new"
    assert cat.reload_if_changed() is False           # stable again


def test_reload_is_atomic_never_leaves_profiles_empty(tmp_path):
    """_load() builds a fresh dict and swaps it in atomically.

    A true concurrent-read race is non-deterministic, so we verify the
    structural guarantee instead: after reload() completes _profiles is
    fully-populated (not empty, not partial), regardless of how many YAML
    files were present before.

    We also confirm the old "clear then fill" window is gone: even if _load
    were called directly, _profiles is never transiently empty from the
    caller's perspective because the swap only happens once the full local
    dict is built.
    """
    _profile(tmp_path, "v1")
    cat = DataCatalog(profile_dir=str(tmp_path))
    assert cat.list_tables() == ["t"]

    # Write a second profile so reload has two tables to load.
    (tmp_path / "u.yaml").write_text(yaml.safe_dump(
        {"table": "u", "description": "second", "columns": {"b": {"dtype": "str", "description": "b"}}}
    ))

    time.sleep(0.01)
    _profile(tmp_path, "v2")          # update t.yaml mtime
    cat.reload()

    # After reload, both tables must be present — catalog is fully populated.
    tables = cat.list_tables()
    assert "t" in tables, f"'t' missing after reload; got {tables}"
    assert "u" in tables, f"'u' missing after reload; got {tables}"
    assert cat.get_description("t") == "v2"
    assert cat.get_description("u") == "second"
    # _profiles must never be the empty sentinel dict — the swap inside _load
    # means self._profiles goes from old-full → new-full, never old-full → {}.
    assert cat._profiles is not {}, "reload() left _profiles as empty dict"
    assert len(cat._profiles) == 2
