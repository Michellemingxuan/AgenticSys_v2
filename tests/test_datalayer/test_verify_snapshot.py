# tests/test_datalayer/test_verify_snapshot.py
"""TDD tests for datalayer.verify_snapshot.

ALL tests use tmp_path dirs — never the real context/ or config/data_profiles/.
The conftest.py autouse guard ensures no test mutates real context/*.txt files.
"""
import hashlib
import json
import os
import pathlib
import pytest

from datalayer.verify_snapshot import (
    cmd_snapshot,
    cmd_list,
    cmd_diff,
    cmd_restore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _make_env(tmp_path: pathlib.Path):
    """Create a minimal fake environment under tmp_path."""
    ctx = tmp_path / "context"
    ctx.mkdir()
    profiles = tmp_path / "config" / "data_profiles"
    profiles.mkdir(parents=True)
    snap_root = tmp_path / ".catalog_verified"
    snap_root.mkdir()
    provenance = profiles / ".provenance.json"

    # Write fixtures
    (ctx / "alpha.txt").write_text("hello alpha\n")
    (ctx / "beta.txt").write_text("hello beta\n")
    (profiles / "model_scores.yaml").write_text("table: model_scores\n")
    provenance.write_text(json.dumps({"source": "test"}))

    return {
        "context_dir": str(ctx),
        "profile_dir": str(profiles),
        "provenance_path": str(provenance),
        "snapshot_root": str(snap_root),
    }


# ---------------------------------------------------------------------------
# snapshot tests
# ---------------------------------------------------------------------------

def test_snapshot_creates_folder_and_manifest(tmp_path):
    env = _make_env(tmp_path)
    result = cmd_snapshot(label="testlabel", **env)

    snap_dir = pathlib.Path(result["snapshot_dir"])
    assert snap_dir.exists(), "snapshot dir must be created"
    assert "testlabel" in snap_dir.name

    manifest = json.loads((snap_dir / "manifest.json").read_text())
    assert manifest["label"] == "testlabel"
    assert "created_utc" in manifest
    assert "files" in manifest
    assert "git_commit" in manifest


def test_snapshot_copies_context_and_profiles(tmp_path):
    env = _make_env(tmp_path)
    result = cmd_snapshot(**env)

    snap_dir = pathlib.Path(result["snapshot_dir"])
    # Context files
    assert (snap_dir / "context" / "alpha.txt").exists()
    assert (snap_dir / "context" / "beta.txt").exists()
    # Profile yaml
    assert (snap_dir / "config" / "data_profiles" / "model_scores.yaml").exists()
    # Provenance
    assert (snap_dir / "config" / "data_profiles" / ".provenance.json").exists()


def test_snapshot_sha256_round_trip(tmp_path):
    env = _make_env(tmp_path)
    result = cmd_snapshot(**env)

    snap_dir = pathlib.Path(result["snapshot_dir"])
    manifest = json.loads((snap_dir / "manifest.json").read_text())

    # Every listed file must have a matching sha on disk
    for relpath, expected_sha in manifest["files"].items():
        actual_sha = _sha256(snap_dir / relpath)
        assert actual_sha == expected_sha, f"sha mismatch for {relpath}"


def test_snapshot_no_label_omits_suffix(tmp_path):
    env = _make_env(tmp_path)
    result = cmd_snapshot(label=None, **env)
    snap_dir = pathlib.Path(result["snapshot_dir"])
    # Name should be just the UTC timestamp (no dash at end)
    assert not snap_dir.name.endswith("-")


def test_snapshot_skips_missing_provenance(tmp_path):
    env = _make_env(tmp_path)
    # Remove provenance
    os.remove(env["provenance_path"])
    result = cmd_snapshot(**env)
    snap_dir = pathlib.Path(result["snapshot_dir"])
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    # Provenance key should not appear
    prov_rel = "config/data_profiles/.provenance.json"
    assert prov_rel not in manifest["files"]


# ---------------------------------------------------------------------------
# list tests
# ---------------------------------------------------------------------------

def test_list_returns_created_snapshot(tmp_path):
    env = _make_env(tmp_path)
    snap_result = cmd_snapshot(label="v1", **env)
    listing = cmd_list(**env)

    names = [s["name"] for s in listing]
    assert pathlib.Path(snap_result["snapshot_dir"]).name in names


def test_list_empty_when_no_snapshots(tmp_path):
    env = _make_env(tmp_path)
    listing = cmd_list(**env)
    assert listing == []


def test_list_multiple_snapshots_sorted(tmp_path):
    env = _make_env(tmp_path)
    cmd_snapshot(label="first", **env)
    import time; time.sleep(1.1)  # ensure distinct 1-second timestamp in dir name
    cmd_snapshot(label="second", **env)
    listing = cmd_list(**env)
    assert len(listing) == 2
    # newest-first order: second should be at index 0
    assert "second" in listing[0]["name"]
    assert "first" in listing[1]["name"]


def test_list_entry_has_required_fields(tmp_path):
    env = _make_env(tmp_path)
    cmd_snapshot(label="check", **env)
    listing = cmd_list(**env)
    entry = listing[0]
    assert "name" in entry
    assert "created_utc" in entry
    assert "label" in entry
    assert "file_count" in entry


# ---------------------------------------------------------------------------
# diff tests
# ---------------------------------------------------------------------------

def test_diff_no_changes_reports_nothing(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name
    result = cmd_diff(target=snap_name, **env)
    assert result["added"] == []
    assert result["removed"] == []
    assert result["changed"] == []


def test_diff_detects_changed_file(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    # Mutate a context file
    alpha_path = pathlib.Path(env["context_dir"]) / "alpha.txt"
    alpha_path.write_text("hello alpha MODIFIED\n")

    result = cmd_diff(target=snap_name, **env)
    changed_files = [c["file"] for c in result["changed"]]
    assert any("alpha.txt" in f for f in changed_files)


def test_diff_changed_file_includes_line_diff(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    alpha_path = pathlib.Path(env["context_dir"]) / "alpha.txt"
    alpha_path.write_text("hello alpha MODIFIED\n")

    result = cmd_diff(target=snap_name, **env)
    changed = result["changed"]
    assert len(changed) == 1
    assert "diff" in changed[0]
    assert changed[0]["diff"]  # non-empty


def test_diff_detects_added_file(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    # Add a new file to context
    (pathlib.Path(env["context_dir"]) / "gamma.txt").write_text("new file\n")

    result = cmd_diff(target=snap_name, **env)
    assert any("gamma.txt" in f for f in result["added"])


def test_diff_detects_removed_file(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    # Remove a file from context
    os.remove(pathlib.Path(env["context_dir"]) / "beta.txt")

    result = cmd_diff(target=snap_name, **env)
    assert any("beta.txt" in f for f in result["removed"])


def test_diff_latest_uses_most_recent_snapshot(tmp_path):
    env = _make_env(tmp_path)
    cmd_snapshot(label="old", **env)
    import time; time.sleep(1.1)  # ensure distinct 1-second timestamp in dir name
    cmd_snapshot(label="new", **env)

    # Diff against "latest" should pick "new"
    result = cmd_diff(target="latest", **env)
    assert result["snapshot_used"].endswith("-new") or "new" in result["snapshot_used"]


# ---------------------------------------------------------------------------
# restore tests
# ---------------------------------------------------------------------------

def test_restore_dry_run_does_not_write(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    # Mutate a file so restore would have something to do
    alpha_path = pathlib.Path(env["context_dir"]) / "alpha.txt"
    original_content = alpha_path.read_text()
    alpha_path.write_text("TAMPERED\n")

    # Dry-run (default, force=False)
    result = cmd_restore(snapshot=snap_name, force=False, **env)
    assert result["dry_run"] is True
    # File should still be tampered (not written back)
    assert alpha_path.read_text() == "TAMPERED\n"
    # Plan should mention alpha.txt
    plan_files = [p["dest"] for p in result["plan"]]
    assert any("alpha.txt" in f for f in plan_files)


def test_restore_dry_run_returns_plan(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    result = cmd_restore(snapshot=snap_name, force=False, **env)
    assert "plan" in result
    assert isinstance(result["plan"], list)
    # All snapshotted files should be in plan
    assert len(result["plan"]) > 0


def test_restore_force_writes_files(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    # Tamper current files
    alpha_path = pathlib.Path(env["context_dir"]) / "alpha.txt"
    alpha_path.write_text("TAMPERED\n")
    beta_path = pathlib.Path(env["context_dir"]) / "beta.txt"
    beta_path.write_text("ALSO TAMPERED\n")

    result = cmd_restore(snapshot=snap_name, force=True, **env)
    assert result["dry_run"] is False
    assert alpha_path.read_text() == "hello alpha\n"
    assert beta_path.read_text() == "hello beta\n"


def test_restore_force_restores_deleted_file(tmp_path):
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    # Delete a file
    alpha_path = pathlib.Path(env["context_dir"]) / "alpha.txt"
    alpha_path.unlink()

    cmd_restore(snapshot=snap_name, force=True, **env)
    assert alpha_path.exists()
    assert alpha_path.read_text() == "hello alpha\n"


def test_restore_dry_run_does_not_touch_real_context(tmp_path):
    """Extra safety: restore dry-run in a tmp env never touches real context/."""
    # This test purely exercises tmp dirs — the conftest guard ensures no real
    # context/ mutation. We just verify that all paths in the plan are under
    # our tmp_path, not the real project root.
    env = _make_env(tmp_path)
    snap = cmd_snapshot(**env)
    snap_name = pathlib.Path(snap["snapshot_dir"]).name

    result = cmd_restore(snapshot=snap_name, force=False, **env)
    for item in result["plan"]:
        dest = pathlib.Path(item["dest"])
        assert str(tmp_path) in str(dest), (
            f"restore plan contains a path outside tmp_path: {dest}"
        )
