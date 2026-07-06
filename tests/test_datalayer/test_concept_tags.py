import yaml
from datalayer.generator import DataGenerator

TAXONOMY = {
    "internal_delinquency", "external_delinquency", "exposure_leverage",
    "capacity_paydown", "oop", "spend_pattern", "trends_tenure",
    "bureau_derived", "risk_events", "output_score", "third_party_score",
}
DATE_OR_TXN_KEYS = {
    "trans_month", "txn_date_time", "trans_dt", "index",
    "appr_deny_cd", "auto_decline_pos_deny_cd_s1",
}

def _cols(path):
    return yaml.safe_load(open(path))["columns"]

def test_every_modeling_variable_has_a_valid_concept():
    for path in ("config/data_profiles/model_scores.yaml",
                 "config/data_profiles/model_scores_transaction.yaml"):
        for name, spec in _cols(path).items():
            if name in DATE_OR_TXN_KEYS:
                continue
            c = spec.get("concept")
            assert c is not None, f"{path}:{name} missing concept"
            cset = {c} if isinstance(c, str) else set(c)
            assert cset <= TAXONOMY, f"{path}:{name} bad concept {cset - TAXONOMY}"

def test_concept_key_is_non_breaking_for_generator():
    g = DataGenerator(profile_dir="config/data_profiles", seed=42, cases=2)
    g.load_profiles(); g.generate_all()
    assert "oop_interaction" in g._tables["model_scores"]

def test_concept_key_absent_from_schema():
    # the schema surface must never expose the raw concept key per column
    from datalayer.catalog import DataCatalog
    cat = DataCatalog(profile_dir="config/data_profiles")
    schema = cat.get_schema("model_scores")  # no active case → catalog schema
    assert schema is not None
    for col, entry in schema.items():
        assert "concept" not in entry, f"{col} leaked concept key into schema"
