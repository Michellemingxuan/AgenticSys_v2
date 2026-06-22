# tests/test_datalayer/test_reconcile.py
from datalayer.gateway import LocalDataGateway
from datalayer.reconcile import check_consistency


def _gw(case_data):
    return LocalDataGateway(case_data=case_data)


def test_consistency_uniform_no_flags():
    gw = _gw({
        "c1": {"t": [{"a": 1, "b": 2}]},
        "c2": {"t": [{"a": 9, "b": 8}]},
    })
    res = check_consistency(gw)
    assert res.flags == []
    assert res.uniform_schema == {"t": {"a", "b"}}


def test_consistency_divergent_table_is_flagged_and_excluded():
    gw = _gw({
        "c1": {"t": [{"a": 1, "b": 2}]},
        "c2": {"t": [{"a": 9}]},          # missing column b
    })
    res = check_consistency(gw)
    assert "t" not in res.uniform_schema
    assert any("t" in f and "c2" in f for f in res.flags)


import pytest
from datalayer.context_dict import ContextEntry
from datalayer.reconcile import reconcile, ReconcileResult
from datalayer.provenance import Provenance


class _Catalog:
    """Minimal stand-in: one table 'model_scores' with column 'credit_loss_prob'."""
    def __init__(self):
        self.patches = []
        self._profiles = {"model_scores": {"table": "model_scores",
                          "columns": {"credit_loss_prob": {"dtype": "float", "description": "old"}}}}
    def list_tables(self): return ["model_scores"]
    def column_aliases(self, t): return {}
    def get_schema(self, t):
        return {c: {"type": s["dtype"], "description": s.get("description", "")}
                for c, s in self._profiles[t]["columns"].items()}
    def write_profile_patch(self, table, patch):
        self.patches.append((table, patch))
        self._profiles[table]["columns"]["credit_loss_prob"].update(
            patch["columns"]["credit_loss_prob"])


class _Agent:  # deterministic stub (no real LLM)
    async def polish_description(self, v, raw, brief): return f"polished: {raw}"
    async def match_column(self, *a, **k): return {"canonical_col": None, "confidence": 0.0}
    async def normalize_threshold_text(self, t): return None


@pytest.mark.asyncio
async def test_reconcile_writes_context_threshold_and_polished_desc(tmp_path):
    from datalayer.gateway import LocalDataGateway
    gw = LocalDataGateway(case_data={"c1": {"model_scores": [{"credit_loss_prob": "55"}]}})
    cat = _Catalog()
    pv = Provenance(str(tmp_path / ".provenance.json"))
    ctx = {"model_scores": {"credit_loss_prob": ContextEntry(
        "credit_loss_prob", "vague desc", "Scores from 10-100 are risky.",
        threshold={"risk_threshold": [10.0, 100.0], "risk_direction": "range"})}}

    res = await reconcile(gw, cat, _Agent(), ctx, pv)

    assert isinstance(res, ReconcileResult)
    spec = cat._profiles["model_scores"]["columns"]["credit_loss_prob"]
    assert spec["risk_threshold"] == [10.0, 100.0]      # gold threshold written verbatim
    assert spec["description"] == "polished: vague desc" # description polished


@pytest.mark.asyncio
async def test_reconcile_skips_human_edited_field(tmp_path):
    from datalayer.gateway import LocalDataGateway
    gw = LocalDataGateway(case_data={"c1": {"model_scores": [{"credit_loss_prob": "55"}]}})
    cat = _Catalog()
    cat._profiles["model_scores"]["columns"]["credit_loss_prob"]["description"] = "HUMAN EDIT"
    pv = Provenance(str(tmp_path / ".provenance.json"))
    pv.record("model_scores", "credit_loss_prob", "description", "agent-wrote-this-earlier")
    ctx = {"model_scores": {"credit_loss_prob": ContextEntry(
        "credit_loss_prob", "new desc", None, threshold=None)}}

    res = await reconcile(gw, cat, _Agent(), ctx, pv, context_dir=str(tmp_path))
    # description was human-edited (current != baseline) → not overwritten, flagged
    assert cat._profiles["model_scores"]["columns"]["credit_loss_prob"]["description"] == "HUMAN EDIT"
    assert any("human" in f.lower() for f in res.flags)


@pytest.mark.asyncio
async def test_reconcile_normalized_lookup_title_case_context(tmp_path):
    """reconcile() must find a context entry keyed 'FICO Score' when the
    profile column is 'fico_score' — via normalized matching."""
    from datalayer.gateway import LocalDataGateway
    from datalayer.reconcile import reconcile, ReconcileResult
    from datalayer.provenance import Provenance
    from datalayer.context_dict import ContextEntry

    # Gateway: one case with a 'bureau' table having 'fico_score'
    gw = LocalDataGateway(case_data={"c1": {"bureau": [{"fico_score": "700"}]}})

    # Catalog: bureau table with fico_score column
    class _BureauCatalog:
        def __init__(self):
            self.patches = []
            self._profiles = {"bureau": {
                "table": "bureau",
                "columns": {"fico_score": {"dtype": "int", "description": "old desc"}}
            }}
        def list_tables(self): return ["bureau"]
        def column_aliases(self, t): return {}
        def get_schema(self, t):
            return {c: {"type": s["dtype"], "description": s.get("description", "")}
                    for c, s in self._profiles[t]["columns"].items()}
        def write_profile_patch(self, table, patch):
            self.patches.append((table, patch))
            self._profiles[table]["columns"]["fico_score"].update(
                patch["columns"].get("fico_score", {}))

    cat = _BureauCatalog()
    pv = Provenance(str(tmp_path / ".prov.json"))

    # Context keyed by Title-Case 'FICO Score' (as in bureau_context_description.txt)
    ctx = {"bureau": {"FICO Score": ContextEntry(
        "FICO Score", "credit risk score", "Values below 721 are risky.",
        threshold={"risk_threshold": 721.0, "risk_direction": "below"})}}

    res = await reconcile(gw, cat, _Agent(), ctx, pv, context_dir=str(tmp_path))

    # The normalized lookup must have found the entry; threshold should be written
    spec = cat._profiles["bureau"]["columns"]["fico_score"]
    assert spec.get("risk_threshold") == 721.0, (
        f"Expected risk_threshold=721.0 from normalized context lookup; got {spec}"
    )
    # Must NOT have a [table-only] flag for fico_score
    assert not any("table-only" in f and "fico_score" in f for f in res.flags), (
        f"fico_score was flagged [table-only] despite matching 'FICO Score' via normalization; flags={res.flags}"
    )


@pytest.mark.asyncio
async def test_reconcile_reverse_syncs_human_field_to_context(tmp_path, monkeypatch):
    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling": ["model_scores"]})
    ctxdir = tmp_path / "context"; ctxdir.mkdir()
    (ctxdir / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: stale desc. Values above 1 are risky.\n")
    from datalayer.gateway import LocalDataGateway
    gw = LocalDataGateway(case_data={"c1": {"model_scores": [{"credit_loss_prob": "55"}]}})
    cat = _Catalog()
    cat._profiles["model_scores"]["columns"]["credit_loss_prob"]["description"] = "HUMAN DESC"
    from datalayer.provenance import Provenance
    pv = Provenance(str(tmp_path / ".prov.json"))
    pv.record("model_scores", "credit_loss_prob", "description", "agent-wrote-earlier")
    from datalayer.context_dict import ContextEntry
    ctx = {"model_scores": {"credit_loss_prob": ContextEntry(
        "credit_loss_prob", "stale desc", None, threshold=None)}}
    res = await reconcile(gw, cat, _Agent(), ctx, pv, context_dir=str(ctxdir))
    line = (ctxdir / "modeling_context_description.txt").read_text().splitlines()[0]
    assert "HUMAN DESC" in line                       # human edit pushed back to context
    assert ("model_scores", "credit_loss_prob") in res.context_writes
