import pytest
from datalayer.context_dict import parse_context_file, ContextEntry, normalize_threshold

SAMPLE = """Data Description
You are a risk analyst. Analyze the case.
1. tpf_internal_delinq_idx: Internal Delinquency Index. Values above 5.8 are considered risky.
2. cust_lndexpsr_minloc_6m_ratio: customer lending exposure minloc 6 months ratio
3. credit_loss_prob: ML model score predicting default. Scores from 10-100 are considered risky.
"""

def test_parse_context_file_extracts_entries(tmp_path):
    p = tmp_path / "modeling_context_description.txt"
    p.write_text(SAMPLE)
    entries = parse_context_file(str(p))
    by_name = {e.var_name: e for e in entries}

    assert set(by_name) == {
        "tpf_internal_delinq_idx",
        "cust_lndexpsr_minloc_6m_ratio",
        "credit_loss_prob",
    }
    # Threshold sentence captured separately from the description.
    assert by_name["tpf_internal_delinq_idx"].threshold_text == "Values above 5.8 are considered risky."
    assert "Internal Delinquency Index" in by_name["tpf_internal_delinq_idx"].raw_description
    # Line with no threshold sentence → threshold_text is None.
    assert by_name["cust_lndexpsr_minloc_6m_ratio"].threshold_text is None
    # Preamble lines (no "N. name:" shape) are ignored.
    assert all(isinstance(e, ContextEntry) for e in entries)
    # Exact-value checks for credit_loss_prob: the keyword "score" in the description
    # ("ML model score predicting default.") must NOT cause the regex to bleed across
    # sentence boundaries and absorb the description into the threshold match.
    assert by_name["credit_loss_prob"].threshold_text == "Scores from 10-100 are considered risky.", (
        f"Got threshold_text={by_name['credit_loss_prob'].threshold_text!r}; "
        "cross-sentence over-match bug present"
    )
    assert "Scores from" not in by_name["credit_loss_prob"].raw_description, (
        f"raw_description should not contain threshold sentence; "
        f"got: {by_name['credit_loss_prob'].raw_description!r}"
    )
    assert "ML model score predicting default" in by_name["credit_loss_prob"].raw_description


@pytest.mark.parametrize("text,expected", [
    ("Values above 5.8 are considered risky.", {"risk_threshold": 5.8, "risk_direction": "above"}),
    ("Values below 0.46 are risky", {"risk_threshold": 0.46, "risk_direction": "below"}),
    ("Values on or above 1 are risky", {"risk_threshold": 1.0, "risk_direction": "above"}),
    ("Scores from 10-100 are considered risky.", {"risk_threshold": [10.0, 100.0], "risk_direction": "range"}),
    (None, None),
    ("some prose with no numbers", None),
])
def test_normalize_threshold(text, expected):
    assert normalize_threshold(text) == expected


def test_load_context_by_table_missing_dir_returns_empty():
    from datalayer.context_dict import load_context_by_table
    assert load_context_by_table("/nonexistent/path/that/cannot/exist") == {}


def test_load_context_by_table(tmp_path):
    from datalayer.context_dict import CONTEXT_TABLE_MAP, load_context_by_table

    (tmp_path / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: default score. Scores from 10-100 are risky.\n"
    )
    # Monkeypatch the map to point a known stem at two tables.
    import datalayer.context_dict as cd
    cd.CONTEXT_TABLE_MAP = {"modeling": ["model_scores", "model_scores_transaction"]}

    out = load_context_by_table(str(tmp_path))
    assert "model_scores" in out and "model_scores_transaction" in out
    entry = out["model_scores"]["credit_loss_prob"]
    assert entry.threshold == {"risk_threshold": [10.0, 100.0], "risk_direction": "range"}


@pytest.mark.parametrize("thr,expected", [
    ({"risk_threshold": 5.8, "risk_direction": "above"}, "Values above 5.8 are risky."),
    ({"risk_threshold": 0.46, "risk_direction": "below"}, "Values below 0.46 are risky."),
    ({"risk_threshold": [10.0, 100.0], "risk_direction": "range"}, "Scores from 10 to 100 are risky."),
    (None, ""),
])
def test_render_threshold(thr, expected):
    from datalayer.context_dict import render_threshold
    assert render_threshold(thr) == expected
