import asyncio
import sqlite3

from tools.node_trace.core import (
    NodeTrace, NodeTraceStore, TurnScope, TURN_SCOPE, _open_node,
)


def _row(store, node):
    conn = sqlite3.connect(store.db_path)
    return conn.execute(
        "SELECT conversation_id, server_run_id, user_id, pillar_id, chat_id "
        "FROM node_trace WHERE node = ?", (node,)).fetchone()


def test_open_node_carries_scope_identity(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    token = TURN_SCOPE.set(TurnScope(
        chat_id="366::u::credit_risk", case_id="366", turn_id="T1",
        conversation_id="366::u::credit_risk", server_run_id="run-A",
        user_id="u", pillar_id="credit_risk"))
    try:
        async def go():
            async with _open_node(store, "root", depth=0):
                pass
        asyncio.run(go())
    finally:
        TURN_SCOPE.reset(token)
    assert _row(store, "root") == (
        "366::u::credit_risk", "run-A", "u", "credit_risk", "366::u::credit_risk")


def test_child_inherits_identity_from_parent(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))

    async def go():
        async with NodeTrace(
            store=store, chat_id="conv-A", case_id="366", turn_id="T1",
            node="parent", depth=0, conversation_id="conv-A",
            server_run_id="run-A", user_id="u", pillar_id="credit_risk"):
            # Child passes only chat_id (as the LLM clients do) — must inherit.
            async with NodeTrace(
                store=store, chat_id="conv-A", case_id="366", turn_id="T1",
                node="child", depth=1):
                pass

    asyncio.run(go())
    assert _row(store, "child") == ("conv-A", "run-A", "u", "credit_risk", "conv-A")


def test_conversation_id_defaults_to_chat_id_when_unset(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))

    async def go():
        async with NodeTrace(store=store, chat_id="legacy-chat",
                             case_id="366", turn_id="T1", node="solo", depth=0):
            pass

    asyncio.run(go())
    row = _row(store, "solo")
    assert row[0] == "legacy-chat"   # conversation_id fell back to chat_id
    assert row[4] == "legacy-chat"
