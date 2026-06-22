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
