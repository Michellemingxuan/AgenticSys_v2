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
