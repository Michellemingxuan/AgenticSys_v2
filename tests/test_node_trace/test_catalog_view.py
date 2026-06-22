"""Tests for build_catalog_view and Provenance.ownership."""
import yaml
import json
from datalayer.provenance import Provenance
from tools.node_trace.catalog_view import build_catalog_view


# ---------------------------------------------------------------------------
# Provenance.ownership tests
# ---------------------------------------------------------------------------

def test_ownership_unmanaged_when_no_baseline(tmp_path):
    pv = Provenance(str(tmp_path / ".prov.json"))
    assert pv.ownership("model_scores", "cbr_score", "description", "anything") == "unmanaged"


def test_ownership_agent_when_baseline_equals_current(tmp_path):
    pv = Provenance(str(tmp_path / ".prov.json"))
    pv.record("model_scores", "cbr_score", "description", "agent text")
    assert pv.ownership("model_scores", "cbr_score", "description", "agent text") == "agent"


def test_ownership_human_when_baseline_differs(tmp_path):
    pv = Provenance(str(tmp_path / ".prov.json"))
    pv.record("model_scores", "cbr_score", "description", "agent text")
    assert pv.ownership("model_scores", "cbr_score", "description", "human edit") == "human"


def test_ownership_roundtrip_after_save(tmp_path):
    p = str(tmp_path / ".prov.json")
    pv = Provenance(p)
    pv.record("t", "c", "risk_threshold", [10.0, 100.0])
    pv.save()
    pv2 = Provenance(p)
    assert pv2.ownership("t", "c", "risk_threshold", [10.0, 100.0]) == "agent"
    assert pv2.ownership("t", "c", "risk_threshold", [5.0, 50.0]) == "human"
    assert pv2.ownership("t", "c", "description", "anything") == "unmanaged"


# ---------------------------------------------------------------------------
# build_catalog_view tests
# ---------------------------------------------------------------------------

def test_build_catalog_view(tmp_path, monkeypatch):
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "model_scores.yaml").write_text(yaml.safe_dump({
        "table": "model_scores",
        "description": "d",
        "columns": {
            "credit_loss_prob": {
                "dtype": "float",
                "description": "x",
                "risk_threshold": [10.0, 100.0],
                "risk_direction": "range",
            }
        }
    }))
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: x. Scores from 10-100 are risky.\n"
        "2. extra_var: y.\n"
    )
    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling": ["model_scores"]})

    pv = Provenance(str(tmp_path / ".prov.json"))
    pv.record("model_scores", "credit_loss_prob", "description", "x")
    pv.save()  # agent-owned (baseline == current)

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    assert "tables" in view
    t = view["tables"][0]
    assert t["table"] == "model_scores"
    assert t["description"] == "d"

    col = t["columns"][0]
    assert col["name"] == "credit_loss_prob"
    assert col["in_context"] is True
    assert col["provenance"] == "agent"
    assert col["threshold"] is not None
    assert col["threshold"]["value"] == [10.0, 100.0]
    assert col["threshold"]["direction"] == "range"
    assert "extra_var" in t["context_only"]


def test_build_catalog_view_human_provenance(tmp_path, monkeypatch):
    """Column where human changed description should get 'human' provenance."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "payments.yaml").write_text(yaml.safe_dump({
        "table": "payments",
        "description": "payments table",
        "columns": {
            "amount": {
                "dtype": "float",
                "description": "human edited description",
            }
        }
    }))
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    pv = Provenance(str(tmp_path / ".prov.json"))
    pv.record("payments", "amount", "description", "original agent description")
    pv.save()

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    assert t["table"] == "payments"
    col = t["columns"][0]
    assert col["provenance"] == "human"
    assert col["threshold"] is None


def test_build_catalog_view_unmanaged_provenance(tmp_path, monkeypatch):
    """Column with no baseline should get 'unmanaged' provenance."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "scores.yaml").write_text(yaml.safe_dump({
        "table": "scores",
        "description": "scores table",
        "columns": {
            "score": {
                "dtype": "int",
                "description": "a score",
            }
        }
    }))
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    pv = Provenance(str(tmp_path / ".prov.json"))
    # No records — nothing recorded

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    col = t["columns"][0]
    assert col["provenance"] == "unmanaged"
    assert col["in_context"] is False


def test_build_catalog_view_table_aliases(tmp_path, monkeypatch):
    """Table-level aliases are surfaced in the view."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "spends.yaml").write_text(yaml.safe_dump({
        "table": "spends",
        "description": "spending data",
        "aliases": ["spend", "txn_spends"],
        "columns": {
            "merchant": {
                "dtype": "str",
                "description": "merchant name",
            }
        }
    }))
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    pv = Provenance(str(tmp_path / ".prov.json"))

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    assert t["aliases"] == ["spend", "txn_spends"]


def test_build_catalog_view_context_only(tmp_path, monkeypatch):
    """context_only lists vars present in context but absent as profile columns."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "model_scores.yaml").write_text(yaml.safe_dump({
        "table": "model_scores",
        "description": "scores",
        "columns": {
            "score_a": {"dtype": "float", "description": "score A"},
        }
    }))
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "modeling_context_description.txt").write_text(
        "1. score_a: description A.\n"
        "2. orphan_var: context only.\n"
        "3. another_orphan: also context only.\n"
    )

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling": ["model_scores"]})

    pv = Provenance(str(tmp_path / ".prov.json"))

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    assert "orphan_var" in t["context_only"]
    assert "another_orphan" in t["context_only"]
    assert "score_a" not in t["context_only"]
    col = t["columns"][0]
    assert col["in_context"] is True


def test_build_catalog_view_parse_hint(tmp_path, monkeypatch):
    """parse_hint is surfaced when present on a column."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "payments.yaml").write_text(yaml.safe_dump({
        "table": "payments",
        "description": "payments",
        "columns": {
            "payment_date": {
                "dtype": "date",
                "description": "date of payment",
                "parse_hint": "D-MMM-YY",
            }
        }
    }))
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    pv = Provenance(str(tmp_path / ".prov.json"))

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    col = t["columns"][0]
    assert col["parse_hint"] == "D-MMM-YY"


def test_build_catalog_view_empty_profile_dir(tmp_path, monkeypatch):
    """Empty profile dir returns empty tables list."""
    prof = tmp_path / "prof"
    prof.mkdir()
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    pv = Provenance(str(tmp_path / ".prov.json"))

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    assert view == {"tables": []}


# ---------------------------------------------------------------------------
# Staleness tests (Change 1)
# ---------------------------------------------------------------------------

def _make_profile_yaml(name, aliases=None):
    spec = {"table": name, "description": "d", "columns": {"col": {"dtype": "int", "description": "x"}}}
    if aliases:
        spec["aliases"] = aliases
    return yaml.safe_dump(spec)


def test_stale_backed_false_unbacked_true(tmp_path, monkeypatch):
    """Profile with a matching CSV is stale=False; profile with no CSV is stale=True."""
    prof = tmp_path / "prof"
    prof.mkdir()
    # 'live_table' will have a CSV backing; 'ghost_table' will not.
    (prof / "live_table.yaml").write_text(_make_profile_yaml("live_table"))
    (prof / "ghost_table.yaml").write_text(_make_profile_yaml("ghost_table"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    # Create a case folder with only live_table.csv
    data_dir = tmp_path / "data"
    case_dir = data_dir / "case001"
    case_dir.mkdir(parents=True)
    (case_dir / "live_table.csv").write_text("col\n1\n2\n")
    # ghost_table has no CSV

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    tables = {t["table"]: t for t in view["tables"]}
    assert tables["live_table"]["stale"] is False
    assert tables["ghost_table"]["stale"] is True


def test_stale_sorts_last(tmp_path, monkeypatch):
    """Stale tables appear after live tables in the returned list."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "alpha.yaml").write_text(_make_profile_yaml("alpha"))       # live
    (prof / "beta.yaml").write_text(_make_profile_yaml("beta"))         # stale
    (prof / "gamma.yaml").write_text(_make_profile_yaml("gamma"))       # live

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    data_dir = tmp_path / "data"
    case_dir = data_dir / "case001"
    case_dir.mkdir(parents=True)
    (case_dir / "alpha.csv").write_text("col\n1\n")
    (case_dir / "gamma.csv").write_text("col\n1\n")
    # beta has no CSV — stale

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    names = [t["table"] for t in view["tables"]]
    # alpha and gamma (live) before beta (stale)
    assert names.index("beta") > names.index("alpha")
    assert names.index("beta") > names.index("gamma")


def test_stale_via_alias(tmp_path, monkeypatch):
    """Profile is live (stale=False) when an alias matches the CSV stem."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "payments.yaml").write_text(_make_profile_yaml("payments", aliases=["payments_success", "payments_returns"]))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    data_dir = tmp_path / "data"
    case_dir = data_dir / "case001"
    case_dir.mkdir(parents=True)
    # Only payments_success.csv present — alias should match
    (case_dir / "payments_success.csv").write_text("payment_date,payment_amount\n2024-01-01,100\n")

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    tables = {t["table"]: t for t in view["tables"]}
    assert tables["payments"]["stale"] is False


def test_stale_none_data_dir_all_false(tmp_path, monkeypatch):
    """data_dir=None → all tables are stale=False (graceful fallback)."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "t1.yaml").write_text(_make_profile_yaml("t1"))
    (prof / "t2.yaml").write_text(_make_profile_yaml("t2"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"), data_dir=None)
    for t in view["tables"]:
        assert t["stale"] is False


def test_stale_missing_data_dir_all_false(tmp_path, monkeypatch):
    """Non-existent data_dir → graceful fallback, all stale=False."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "t1.yaml").write_text(_make_profile_yaml("t1"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(tmp_path / "nonexistent"),
    )
    for t in view["tables"]:
        assert t["stale"] is False
