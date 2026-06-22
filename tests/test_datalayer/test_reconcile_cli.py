# tests/test_datalayer/test_reconcile_cli.py
import pytest
import yaml
import json
from pathlib import Path


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
