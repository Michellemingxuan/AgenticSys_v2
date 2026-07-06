import textwrap
from datalayer.catalog import DataCatalog, CONCEPT_GLOSS

def _catalog(tmp_path):
    (tmp_path / "m.yaml").write_text(textwrap.dedent("""
    table: m
    columns:
      a_ratio:
        dtype: float
        description: A ratio thing. Values above 3.15 are risky.
        risk_threshold: 3.15
        risk_direction: above
        concept: [oop, exposure_leverage]
      b_index:
        dtype: float
        description: B index thing.
        concept: oop
      c_other:
        dtype: int
        description: Unrelated.
        concept: capacity_paydown
    """))
    return DataCatalog(profile_dir=str(tmp_path))

def test_concepts_for_tables(tmp_path):
    cat = _catalog(tmp_path)
    assert cat.concepts_for_tables(["m"]) == ["capacity_paydown", "exposure_leverage", "oop"]

def test_variables_for_concepts_matches_and_renders(tmp_path):
    cat = _catalog(tmp_path)
    got = cat.variables_for_concepts(["m"], ["oop"])
    names = [v["name"] for v in got]
    assert names == ["a_ratio", "b_index"]  # thresholded-first (a_ratio has threshold)
    a = next(v for v in got if v["name"] == "a_ratio")
    assert a["threshold_text"] == "risky > 3.15"
    assert a["description_short"] == "A ratio thing"
    assert cat.variables_for_concepts(["m"], ["capacity_paydown"])[0]["name"] == "c_other"

def test_variables_for_concepts_cap(tmp_path):
    cat = _catalog(tmp_path)
    assert len(cat.variables_for_concepts(["m"], ["oop"], limit=1)) == 1

def test_gloss_covers_taxonomy():
    for c in ("internal_delinquency", "oop", "third_party_score"):
        assert c in CONCEPT_GLOSS
