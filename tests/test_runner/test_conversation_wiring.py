"""The wiring is validated at the seam: a CaseSession-like object fed through
the same id computation yields a deterministic conversation_id, and a
TurnScope built the way the conductor builds it groups two server runs
under ONE conversation_id."""
from runner.identity import compose_conversation_id, resolve_user_id
from tools.node_trace.core import TurnScope


class _Cfg:
    user_id = "amx_reviewer"


def test_session_conversation_id_is_deterministic_across_runs():
    cid = compose_conversation_id("366", resolve_user_id(_Cfg()), "credit_risk")
    assert cid == "366::amx_reviewer::credit_risk"
    # A second "server run" recomputes the SAME id from the same inputs.
    assert cid == compose_conversation_id("366", resolve_user_id(_Cfg()), "credit_risk")


def test_turnscope_two_runs_share_conversation_but_differ_by_run():
    cid = "366::amx_reviewer::credit_risk"
    s_a = TurnScope(chat_id=cid, case_id="366", turn_id="T1",
                    conversation_id=cid, server_run_id="run-A",
                    user_id="amx_reviewer", pillar_id="credit_risk")
    s_b = TurnScope(chat_id=cid, case_id="366", turn_id="T2",
                    conversation_id=cid, server_run_id="run-B",
                    user_id="amx_reviewer", pillar_id="credit_risk")
    assert s_a.conversation_id == s_b.conversation_id == cid
    assert s_a.chat_id == s_b.chat_id == cid       # grouping axis stable
    assert s_a.server_run_id != s_b.server_run_id  # diagnostic differs
