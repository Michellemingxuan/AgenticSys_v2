import importlib

from runner import identity


def test_compose_conversation_id_is_deterministic_and_readable():
    a = identity.compose_conversation_id("366-abc", "amx_reviewer", "credit_risk")
    b = identity.compose_conversation_id("366-abc", "amx_reviewer", "credit_risk")
    assert a == b == "366-abc::amx_reviewer::credit_risk"


def test_compose_varies_by_each_component():
    base = identity.compose_conversation_id("366", "u1", "credit_risk")
    assert base != identity.compose_conversation_id("367", "u1", "credit_risk")
    assert base != identity.compose_conversation_id("366", "u2", "credit_risk")
    assert base != identity.compose_conversation_id("366", "u1", "escalation")


def test_resolve_user_id_prefers_cfg_then_env(monkeypatch):
    class Cfg:
        user_id = "cfg_user"
    assert identity.resolve_user_id(Cfg()) == "cfg_user"
    assert identity.resolve_user_id(None) == "amx_reviewer"
    monkeypatch.setenv("AMEM_USER_ID", "env_user")
    assert identity.resolve_user_id(None) == "env_user"


def test_server_run_id_is_stable_within_process_and_prefixed():
    assert identity.SERVER_RUN_ID.startswith("run-")
    assert identity.SERVER_RUN_ID == importlib.import_module("runner.identity").SERVER_RUN_ID
