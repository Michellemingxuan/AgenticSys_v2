from datalayer.provenance import Provenance


def test_unrecorded_field_is_agent_owned(tmp_path):
    pv = Provenance(str(tmp_path / ".provenance.json"))
    assert pv.is_agent_owned("model_scores", "cbr_score", "description", "anything") is True


def test_recorded_then_unchanged_is_agent_owned(tmp_path):
    pv = Provenance(str(tmp_path / ".provenance.json"))
    pv.record("model_scores", "cbr_score", "description", "agent text")
    assert pv.is_agent_owned("model_scores", "cbr_score", "description", "agent text") is True


def test_recorded_then_human_changed_is_not_agent_owned(tmp_path):
    pv = Provenance(str(tmp_path / ".provenance.json"))
    pv.record("model_scores", "cbr_score", "description", "agent text")
    assert pv.is_agent_owned("model_scores", "cbr_score", "description", "human edit") is False


def test_roundtrip_persists(tmp_path):
    p = str(tmp_path / ".provenance.json")
    pv = Provenance(p)
    pv.record("t", "c", "f", "v")
    pv.save()
    assert Provenance(p).is_agent_owned("t", "c", "f", "v") is True


def test_roundtrip_json_native_types(tmp_path):
    """Guard test: float, list, and string values survive save+reload without corruption."""
    p = str(tmp_path / ".provenance.json")
    pv = Provenance(p)

    # Record task 7 real value types: float threshold, list of floats, string description
    pv.record("t", "c", "risk_threshold", 5.8)
    pv.record("t", "c2", "risk_threshold", [10.0, 100.0])
    pv.record("t", "c3", "description", "some text")
    pv.save()

    # Reload and verify equality with original Python values
    reloaded = Provenance(p)
    assert reloaded.is_agent_owned("t", "c", "risk_threshold", 5.8) is True
    assert reloaded.is_agent_owned("t", "c2", "risk_threshold", [10.0, 100.0]) is True
    assert reloaded.is_agent_owned("t", "c3", "description", "some text") is True
