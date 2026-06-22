# tests/test_datalayer/test_reconcile_cli.py
import asyncio
import sys
import pytest
import yaml
import json
from pathlib import Path
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_run_reconcile_writes_threshold_from_context(tmp_path, monkeypatch):
    # data: one case, table model_scores with column credit_loss_prob
    case = tmp_path / "data" / "c1"
    case.mkdir(parents=True)
    (case / "model_scores.csv").write_text("credit_loss_prob\n55\n")

    # profile
    prof = tmp_path / "profiles"
    prof.mkdir()
    (prof / "model_scores.yaml").write_text(yaml.safe_dump(
        {"table": "model_scores", "description": "",
         "columns": {"credit_loss_prob": {"dtype": "float", "description": "old"}}}))

    # context
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: default score. Scores from 10-100 are risky.\n")

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling": ["model_scores"]})

    from datalayer.sync import run_reconcile
    res = await run_reconcile(str(tmp_path / "data"), str(ctx), str(prof), llm=None)

    written = yaml.safe_load((prof / "model_scores.yaml").read_text())
    spec = written["columns"]["credit_loss_prob"]
    assert spec["risk_threshold"] == [10.0, 100.0]
    # provenance sidecar created
    assert (Path(prof) / ".provenance.json").exists()


@pytest.mark.asyncio
async def test_amain_reconcile_branch_reachable_without_real_data(tmp_path, monkeypatch):
    """Regression: --reconcile must be reachable even when data_tables/real/ is absent.

    Approach: monkeypatch `sync.run_reconcile` with an async spy and invoke
    `amain` with `--reconcile --no-llm --data-dir <tmp>`.  We assert the spy
    was awaited (proving the branch is entered before the sources/sys.exit(1)
    guard), and that `amain` returns without raising SystemExit.

    Why spy rather than full end-to-end: `run_reconcile` needs real CSV files
    and profile YAMLs; those concerns are covered by
    `test_run_reconcile_writes_threshold_from_context`.  Here we only want to
    prove the CLI routing is correct — that `--reconcile` is handled before
    the data-dir guard that would kill CI/fresh-checkout runs.
    """
    import datalayer.sync as sync

    spy_called_with: dict = {}

    async def _fake_run_reconcile(data_dir, context_dir="context",
                                  profile_dir="config/data_profiles", *, llm=None):
        spy_called_with["data_dir"] = data_dir
        spy_called_with["llm"] = llm
        # Return a minimal ReconcileResult-like object
        class _FakeResult:
            writes = []
            flags = ["flag-a", "flag-b"]
        return _FakeResult()

    monkeypatch.setattr(sync, "run_reconcile", _fake_run_reconcile)

    data_dir = str(tmp_path / "my_data")

    # Simulate a fresh-checkout / CI env where data_tables/real/ does NOT exist
    # by pointing _REAL_DIR at a path that definitely doesn't exist.
    monkeypatch.setattr(sync, "_REAL_DIR", tmp_path / "nonexistent_real")
    monkeypatch.setattr(sync, "_SIM_DIR", tmp_path / "nonexistent_sim")

    monkeypatch.setattr(sys, "argv", [
        "sync", "--reconcile", "--no-llm", "--data-dir", data_dir,
    ])

    # Must NOT raise SystemExit (which the sources-check at line ~650 would raise
    # when data_tables/real/ is absent).
    await sync.amain()

    assert spy_called_with, "run_reconcile spy was never awaited — branch unreachable"
    assert spy_called_with["data_dir"] == data_dir, (
        f"data_dir mismatch: got {spy_called_with['data_dir']!r}"
    )
    assert spy_called_with["llm"] is None, "--no-llm should pass llm=None"
