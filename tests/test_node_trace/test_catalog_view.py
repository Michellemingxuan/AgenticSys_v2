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
    assert "context_only" not in t


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


def test_build_catalog_view_no_context_only_key(tmp_path, monkeypatch):
    """context_only key must NOT appear in any table dict — context-only vars are dropped."""
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
    # context_only key must be absent from the table dict
    assert "context_only" not in t
    # in_context for a matched profile column must still work correctly
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


# ---------------------------------------------------------------------------
# Normalized matching: Title-Case context var ↔ snake_case profile column
# ---------------------------------------------------------------------------

def test_build_catalog_view_normalized_match(tmp_path, monkeypatch):
    """Profile column 'fico_score' should match context var 'FICO Score' via normalize_key."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "bureau.yaml").write_text(yaml.safe_dump({
        "table": "bureau",
        "description": "bureau data",
        "columns": {
            "fico_score": {"dtype": "int", "description": "FICO score"},
            "sbfe_score": {"dtype": "int", "description": "SBFE score"},
            "unmatched_col": {"dtype": "str", "description": "no context"},
        }
    }))
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "bureau_context_description.txt").write_text(
        "1. FICO Score: An industry-standard credit risk score. Values below 721 are risky.\n"
        "2. SBFE Score: Business credit score. Values below 863 are risky.\n"
        "3. Orphan Var: Context var with no matching profile column.\n"
    )

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"bureau": ["bureau"]})

    pv = Provenance(str(tmp_path / ".prov.json"))

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    assert t["table"] == "bureau"

    by_name = {c["name"]: c for c in t["columns"]}
    # Normalized match: FICO Score → fico_score → in_context=True
    assert by_name["fico_score"]["in_context"] is True, "fico_score should match 'FICO Score' via normalization"
    assert by_name["sbfe_score"]["in_context"] is True, "sbfe_score should match 'SBFE Score' via normalization"
    # Column with no context var → in_context=False
    assert by_name["unmatched_col"]["in_context"] is False

    # context_only key must NOT appear in the table dict (context-only vars are dropped)
    assert "context_only" not in t


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
    assert view["tables"] == []
    assert view["groups"] == []


# ---------------------------------------------------------------------------
# Staleness tests (Change 1)
# ---------------------------------------------------------------------------

def _make_profile_yaml(name, aliases=None):
    spec = {"table": name, "description": "d", "columns": {"col": {"dtype": "int", "description": "x"}}}
    if aliases:
        spec["aliases"] = aliases
    return yaml.safe_dump(spec)


def test_stale_backed_false_unbacked_true(tmp_path, monkeypatch):
    """Profile with a matching CSV is live (appears in tables); profile with no CSV is excluded."""
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
    # ghost_table has no CSV — excluded

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    table_names = [t["table"] for t in view["tables"]]
    assert "live_table" in table_names
    assert view["tables"][0]["stale"] is False
    # ghost_table excluded entirely — not in tables or groups
    assert "ghost_table" not in table_names
    assert "ghost_table" not in [g["key"] for g in view["groups"]]


def test_stale_sorts_last(tmp_path, monkeypatch):
    """Stale tables are excluded entirely; only live tables appear in the result."""
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
    # beta has no CSV — excluded

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    names = [t["table"] for t in view["tables"]]
    # alpha and gamma (live) present; beta (stale) excluded entirely
    assert "alpha" in names
    assert "gamma" in names
    assert "beta" not in names


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


# ---------------------------------------------------------------------------
# Grouping tests (monthly + transaction variants share a group)
# ---------------------------------------------------------------------------

def test_grouping_model_scores_and_transaction_share_group(tmp_path, monkeypatch):
    """model_scores and model_scores_transaction must be in the same group."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "model_scores.yaml").write_text(_make_profile_yaml("model_scores"))
    (prof / "model_scores_transaction.yaml").write_text(_make_profile_yaml("model_scores_transaction"))
    (prof / "standalone.yaml").write_text(_make_profile_yaml("standalone"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))

    # The view must include a 'groups' key.
    assert "groups" in view, "build_catalog_view must return a 'groups' key"

    groups = view["groups"]
    # Find the group that contains model_scores.
    ms_group = next(
        (g for g in groups if any(t["table"] == "model_scores" for t in g["tables"])),
        None,
    )
    assert ms_group is not None, "model_scores should be in a group"
    group_table_names = [t["table"] for t in ms_group["tables"]]
    assert "model_scores" in group_table_names
    assert "model_scores_transaction" in group_table_names, (
        "model_scores_transaction should share a group with model_scores"
    )

    # standalone has no transaction variant → it's its own group.
    standalone_group = next(
        (g for g in groups if any(t["table"] == "standalone" for t in g["tables"])),
        None,
    )
    assert standalone_group is not None
    assert len(standalone_group["tables"]) == 1


def test_grouping_base_table_before_transaction(tmp_path, monkeypatch):
    """Within a group, the base (monthly) table must come before the _transaction one."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "score_drivers.yaml").write_text(_make_profile_yaml("score_drivers"))
    (prof / "score_drivers_transaction.yaml").write_text(_make_profile_yaml("score_drivers_transaction"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))

    sd_group = next(
        (g for g in view["groups"] if any(t["table"] == "score_drivers" for t in g["tables"])),
        None,
    )
    assert sd_group is not None
    names = [t["table"] for t in sd_group["tables"]]
    assert names.index("score_drivers") < names.index("score_drivers_transaction"), (
        "base table must appear before _transaction variant within its group"
    )


def test_grouping_tables_list_members_are_adjacent(tmp_path, monkeypatch):
    """In the top-level 'tables' list, grouped members must be adjacent."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "model_scores.yaml").write_text(_make_profile_yaml("model_scores"))
    (prof / "model_scores_transaction.yaml").write_text(_make_profile_yaml("model_scores_transaction"))
    (prof / "payments.yaml").write_text(_make_profile_yaml("payments"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    names = [t["table"] for t in view["tables"]]

    idx_base = names.index("model_scores")
    idx_txn = names.index("model_scores_transaction")
    # They must be adjacent (differ by exactly 1, base first).
    assert idx_txn == idx_base + 1, (
        f"model_scores_transaction (pos {idx_txn}) must immediately follow "
        f"model_scores (pos {idx_base})"
    )


def test_grouping_group_key_assigned(tmp_path, monkeypatch):
    """Each table dict must carry a 'group_key' field with the correct value."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "model_scores.yaml").write_text(_make_profile_yaml("model_scores"))
    (prof / "model_scores_transaction.yaml").write_text(_make_profile_yaml("model_scores_transaction"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    by_name = {t["table"]: t for t in view["tables"]}

    assert by_name["model_scores"]["group_key"] == "model_scores"
    assert by_name["model_scores_transaction"]["group_key"] == "model_scores"


def test_grouping_all_stale_group_sorts_last(tmp_path, monkeypatch):
    """An all-stale group (both base + transaction stale) is excluded entirely; live group remains."""
    prof = tmp_path / "prof"
    prof.mkdir()
    # live standalone table
    (prof / "live_table.yaml").write_text(_make_profile_yaml("live_table"))
    # stale pair: no backing CSVs
    (prof / "stale_base.yaml").write_text(_make_profile_yaml("stale_base"))
    (prof / "stale_base_transaction.yaml").write_text(_make_profile_yaml("stale_base_transaction"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    data_dir = tmp_path / "data"
    (data_dir / "case001").mkdir(parents=True)
    (data_dir / "case001" / "live_table.csv").write_text("col\n1\n")

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )

    groups = view["groups"]
    group_keys = [g["key"] for g in groups]
    # The live group is present
    assert "live_table" in group_keys
    # The stale group is excluded entirely
    assert "stale_base" not in group_keys
    # Only one group remains
    assert len(groups) == 1
    live_group = groups[0]
    assert live_group.get("all_stale") is False


def test_grouping_standalone_is_own_group(tmp_path, monkeypatch):
    """A table with no _transaction sibling is its own single-member group."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "payments.yaml").write_text(_make_profile_yaml("payments"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))

    assert len(view["groups"]) == 1
    g = view["groups"][0]
    assert len(g["tables"]) == 1
    assert g["tables"][0]["table"] == "payments"
    assert g["key"] == "payments"


def test_grouping_existing_tables_key_unchanged(tmp_path, monkeypatch):
    """The existing 'tables' key must still be present and contain all tables."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "model_scores.yaml").write_text(_make_profile_yaml("model_scores"))
    (prof / "model_scores_transaction.yaml").write_text(_make_profile_yaml("model_scores_transaction"))
    (prof / "payments.yaml").write_text(_make_profile_yaml("payments"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))

    # Existing key must remain
    assert "tables" in view
    assert len(view["tables"]) == 3


# ---------------------------------------------------------------------------
# Change 3 — Stale profiles EXCLUDED entirely from tables and groups
# ---------------------------------------------------------------------------

def test_stale_excluded_from_tables_and_groups(tmp_path, monkeypatch):
    """Stale tables (no backing CSV) are EXCLUDED from both 'tables' and 'groups'."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "live_table.yaml").write_text(_make_profile_yaml("live_table"))
    (prof / "ghost_table.yaml").write_text(_make_profile_yaml("ghost_table"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    data_dir = tmp_path / "data"
    (data_dir / "case001").mkdir(parents=True)
    (data_dir / "case001" / "live_table.csv").write_text("col\n1\n")
    # ghost_table has no CSV — stale

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    table_names = [t["table"] for t in view["tables"]]
    group_keys = [g["key"] for g in view["groups"]]

    assert "ghost_table" not in table_names, "stale table must be excluded from 'tables'"
    assert "ghost_table" not in group_keys, "stale table must be excluded from 'groups'"
    assert "live_table" in table_names, "live table must still be present"


def test_stale_pair_excluded_when_both_stale(tmp_path, monkeypatch):
    """A paired group (base + _transaction) with no backing CSVs is excluded entirely."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "live.yaml").write_text(_make_profile_yaml("live"))
    (prof / "stale_base.yaml").write_text(_make_profile_yaml("stale_base"))
    (prof / "stale_base_transaction.yaml").write_text(_make_profile_yaml("stale_base_transaction"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    data_dir = tmp_path / "data"
    (data_dir / "case001").mkdir(parents=True)
    (data_dir / "case001" / "live.csv").write_text("col\n1\n")

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    table_names = [t["table"] for t in view["tables"]]
    group_keys = [g["key"] for g in view["groups"]]

    assert "stale_base" not in table_names
    assert "stale_base_transaction" not in table_names
    assert "stale_base" not in group_keys
    assert "live" in table_names


def test_stale_excluded_data_dir_none_all_present(tmp_path, monkeypatch):
    """data_dir=None → nothing is stale → all tables appear in tables and groups."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "t1.yaml").write_text(_make_profile_yaml("t1"))
    (prof / "t2.yaml").write_text(_make_profile_yaml("t2"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"), data_dir=None)
    table_names = [t["table"] for t in view["tables"]]
    assert "t1" in table_names
    assert "t2" in table_names
    assert len(view["groups"]) == 2


def test_stale_groups_base_first_after_exclusion(tmp_path, monkeypatch):
    """After excluding stale entries, a live group with base + transaction still has base first."""
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "scores.yaml").write_text(_make_profile_yaml("scores"))
    (prof / "scores_transaction.yaml").write_text(_make_profile_yaml("scores_transaction"))
    (prof / "ghost.yaml").write_text(_make_profile_yaml("ghost"))

    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    data_dir = tmp_path / "data"
    (data_dir / "case001").mkdir(parents=True)
    (data_dir / "case001" / "scores.csv").write_text("col\n1\n")
    (data_dir / "case001" / "scores_transaction.csv").write_text("col\n1\n")

    view = build_catalog_view(
        str(prof), str(ctx), str(tmp_path / ".prov.json"),
        data_dir=str(data_dir),
    )
    scores_group = next(g for g in view["groups"] if g["key"] == "scores")
    names = [t["table"] for t in scores_group["tables"]]
    assert names[0] == "scores"
    assert names[1] == "scores_transaction"
    assert "ghost" not in [t["table"] for t in view["tables"]]


# ---------------------------------------------------------------------------
# Change 1 — description_short: truncate long descriptions
# ---------------------------------------------------------------------------

def _make_profile_with_description(tmp_path, name, description):
    spec = {
        "table": name,
        "description": description,
        "columns": {"col": {"dtype": "int", "description": "x"}},
    }
    return yaml.safe_dump(spec)


def test_description_short_truncates_at_140_chars(tmp_path, monkeypatch):
    """Long description (>140 chars, no early sentence end) gets a description_short truncated to ~140 chars + ellipsis."""
    long_desc = "A" * 200  # no sentence end, 200 chars
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "t.yaml").write_text(_make_profile_with_description(tmp_path, "t", long_desc))
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    # description must remain full
    assert t["description"] == long_desc
    # description_short must be present and shorter
    assert "description_short" in t
    short = t["description_short"]
    assert len(short) <= 145, f"description_short too long: {len(short)}"
    assert short.endswith("…") or short.endswith("..."), "must end with ellipsis"


def test_description_short_uses_first_sentence_when_shorter(tmp_path, monkeypatch):
    """When the first sentence is < 140 chars, description_short uses the first sentence (not full text)."""
    # First sentence is 30 chars; total desc is > 140 chars so truncation IS triggered.
    first_sentence = "First sentence."
    rest = " " + "B" * 150  # make total > 140
    desc = first_sentence + rest
    assert len(desc) > 140, "test precondition: description must exceed 140 chars"
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "t.yaml").write_text(_make_profile_with_description(tmp_path, "t", desc))
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    assert t["description"] == desc
    short = t["description_short"]
    # Should be at most first sentence + ellipsis
    assert "First sentence" in short
    assert len(short) < len(desc)
    assert short.endswith("…") or short.endswith("...")


def test_description_short_no_truncation_for_short_desc(tmp_path, monkeypatch):
    """Short descriptions (< 140 chars) get description_short equal to description (no ellipsis)."""
    short_desc = "Short description."
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "t.yaml").write_text(_make_profile_with_description(tmp_path, "t", short_desc))
    ctx = tmp_path / "ctx"
    ctx.mkdir()

    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {})

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path / ".prov.json"))
    t = view["tables"][0]
    assert t["description_short"] == short_desc
