from datalayer.context_dict import parse_context_file, ContextEntry

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
