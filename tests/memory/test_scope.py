from memory.config import AmemConfig
from memory.scope import build_scope, base_metadata, kps_for_turn, kps_for_agent_turn

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_build_scope_sets_constants_and_fields():
    s = build_scope(CFG, "case_1", turn_id="t9", agent_id="risk")
    assert s.org_id == "amx"
    assert s.user_id == "amx_reviewer"
    assert s.case_id == "case_1"
    assert s.turn_id == "t9"
    assert s.agent_id == "risk"


def test_build_scope_case_only_leaves_turn_agent_none():
    s = build_scope(CFG, "case_1")
    assert s.turn_id is None and s.agent_id is None


def test_base_metadata_omits_none():
    assert base_metadata("sess_1") == {"session_id": "sess_1"}
    assert base_metadata(None) == {}


def test_kps_for_turn_filters_by_captured_turn():
    kps = {
        "risk": [
            {"claim": "A", "captured_at_turn": "t1"},
            {"claim": "B", "captured_at_turn": "t2"},
            {"claim": "", "captured_at_turn": "t1"},   # blank skipped
        ],
        "fraud": [{"claim": "C", "captured_at_turn": "t1"}],
    }
    assert sorted(kps_for_turn(kps, "t1")) == ["A", "C"]
    assert kps_for_turn(kps, "t2") == ["B"]
    assert kps_for_turn(kps, "tX") == []


def test_kps_for_agent_turn_returns_full_dicts_for_one_agent():
    kps = {
        "risk": [
            {"claim": "A", "captured_at_turn": "t1", "topic": "x"},
            {"claim": "B", "captured_at_turn": "t2", "topic": "y"},
        ],
        "fraud": [{"claim": "C", "captured_at_turn": "t1", "topic": "z"}],
    }
    assert kps_for_agent_turn(kps, "risk", "t1") == [
        {"claim": "A", "captured_at_turn": "t1", "topic": "x"}
    ]
    assert kps_for_agent_turn(kps, "risk", "t2") == [
        {"claim": "B", "captured_at_turn": "t2", "topic": "y"}
    ]
    # different agent's KPs never leak in
    assert kps_for_agent_turn(kps, "fraud", "t2") == []
    # unknown agent -> empty list, no KeyError
    assert kps_for_agent_turn(kps, "unknown", "t1") == []
