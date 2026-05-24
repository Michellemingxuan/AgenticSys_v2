import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.node_trace import NodeTrace, NodeTraceRunHooks, NodeTraceStore


@pytest.mark.asyncio
async def test_hooks_create_round_under_parent(tmp_path: Path):
    """Calling on_llm_start + on_llm_end manually should INSERT a child
    NodeTrace row under the bound parent, regardless of contextvar state.
    """
    store = NodeTraceStore(str(tmp_path / "h.db"))

    async with NodeTrace(
        store, chat_id="c", case_id="x", turn_id="T",
        node="orchestrator", depth=0,
    ) as parent:
        hooks = NodeTraceRunHooks(store, parent)
        # Fake an LLM call: start, then end with a synthetic ModelResponse
        fake_agent = SimpleNamespace(model="gpt-test")
        await hooks.on_llm_start(
            None, fake_agent, "system prompt here",
            [{"role": "user", "content": "what is the spend?"}],
        )
        fake_response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=120, output_tokens=18, total_tokens=138,
                input_tokens_details={"cached_tokens": 0},
                output_tokens_details=None,
            ),
            to_input_items=lambda: [
                {"type": "function_call",
                 "id": "c1", "call_id": "c1",
                 "name": "batch_aggregate", "arguments": "{}"},
            ],
        )
        await hooks.on_llm_end(None, fake_agent, fake_response)

    conn = sqlite3.connect(str(tmp_path / "h.db"))
    rows = conn.execute(
        "SELECT node, depth, parent_id, prompt_tokens, completion_tokens, "
        "       cached_input_tokens, outcome FROM node_trace ORDER BY id"
    ).fetchall()
    # Parent + 1 round
    assert len(rows) == 2
    assert rows[0][0] == "orchestrator"
    assert rows[1][0] == "orchestrator.round_1"
    assert rows[1][1] == 1          # depth
    assert rows[1][2] == rows[0][0:1][0] or rows[1][2] is not None  # parent_id set
    assert rows[1][3] == 120        # prompt_tokens
    assert rows[1][4] == 18         # completion_tokens
    assert rows[1][6] == "ok"
    # Tool call name should show up in the synthetic output_json
    output_json = conn.execute(
        "SELECT output_json FROM node_trace WHERE node='orchestrator.round_1'"
    ).fetchone()[0]
    assert "batch_aggregate" in output_json


@pytest.mark.asyncio
async def test_hooks_handle_duplicate_start_events(tmp_path: Path):
    """Reproduces the SDK pattern where on_llm_start fires TWICE in rapid
    succession for the same underlying LLM call (once for the streamed
    wrapper, once for the inner call). Each start must get its own row,
    and each end must update the matching row — not overwrite the same
    one and drop the other's data."""
    store = NodeTraceStore(str(tmp_path / "h.db"))

    async with NodeTrace(
        store, chat_id="c", case_id="x", turn_id="T",
        node="orchestrator", depth=0,
    ) as parent:
        hooks = NodeTraceRunHooks(store, parent)
        fake_agent = SimpleNamespace(model="gpt-test")
        # Two starts in a row (the SDK's duplicate-firing pattern).
        await hooks.on_llm_start(None, fake_agent, "sys A", [{"role": "user", "content": "qA"}])
        await hooks.on_llm_start(None, fake_agent, "sys B", [{"role": "user", "content": "qB"}])
        # Two ends in LIFO order matching the start stack.
        await hooks.on_llm_end(None, fake_agent, SimpleNamespace(
            usage=SimpleNamespace(input_tokens=20, output_tokens=5, total_tokens=25,
                                  input_tokens_details=None, output_tokens_details=None),
            to_input_items=lambda: [{"type": "message", "role": "assistant", "content": "B done"}],
        ))
        await hooks.on_llm_end(None, fake_agent, SimpleNamespace(
            usage=SimpleNamespace(input_tokens=10, output_tokens=3, total_tokens=13,
                                  input_tokens_details=None, output_tokens_details=None),
            to_input_items=lambda: [{"type": "message", "role": "assistant", "content": "A done"}],
        ))

    conn = sqlite3.connect(str(tmp_path / "h.db"))
    rows = conn.execute(
        "SELECT node, prompt_tokens, completion_tokens, output_json, extra_json "
        "FROM node_trace WHERE depth = 1 ORDER BY id"
    ).fetchall()
    assert len(rows) == 2, f"expected 2 round rows, got {len(rows)}"
    # Both rows should have output + extras populated (no ghost rows).
    for r in rows:
        assert r[1] is not None, f"prompt_tokens null on {r[0]}"
        assert r[2] is not None, f"completion_tokens null on {r[0]}"
        assert r[3] and "assistant" in r[3], f"output_json missing on {r[0]}: {r[3]!r}"
        assert r[4] and "response_type" in r[4], f"extra_json missing on {r[0]}: {r[4]!r}"


@pytest.mark.asyncio
async def test_hooks_disabled_without_parent(tmp_path: Path):
    """When parent is None / not a NodeTrace, hooks are a no-op."""
    store = NodeTraceStore(str(tmp_path / "h2.db"))
    hooks = NodeTraceRunHooks(store, None)
    await hooks.on_llm_start(None, SimpleNamespace(model="m"), "sys", [])
    await hooks.on_llm_end(None, SimpleNamespace(model="m"), SimpleNamespace(usage=None))
    # No rows
    conn = sqlite3.connect(str(tmp_path / "h2.db"))
    n = conn.execute("SELECT COUNT(*) FROM node_trace").fetchone()[0]
    assert n == 0
