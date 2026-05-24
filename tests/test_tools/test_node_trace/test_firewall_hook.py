import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from tools.node_trace import NodeTrace, NodeTraceStore


@pytest.mark.asyncio
async def test_firewall_client_skips_wrap_when_hooks_own_rounds(tmp_path: Path):
    """When the parent NodeTrace is flagged ``_hooks_own_rounds=True``
    (set by NodeTraceRunHooks), firewall_client must NOT also create a
    child round row. Otherwise every streamed LLM call lands twice."""
    from tools.node_trace import NodeTrace, NodeTraceStore
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    inner = MagicMock()
    inner.chat = SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=2, total_tokens=12,
                prompt_tokens_details=None, completion_tokens_details=None,
            ),
            model="m",
        ))
    ))
    logger = EventLogger(session_id="t", log_dir=str(tmp_path))
    firewall = FirewallStack(logger=logger)
    client = FirewalledAsyncOpenAI(base=inner, firewall=firewall)

    async with NodeTrace(
        store, chat_id="c", case_id="c", turn_id="t",
        node="orchestrator", depth=0,
    ) as parent:
        # Hooks-own-rounds flag set — firewall_client should NOT wrap.
        parent._hooks_own_rounds = True
        await client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}],
        )

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    nodes = [r[0] for r in conn.execute("SELECT node FROM node_trace ORDER BY id")]
    # Only the parent — no child round row created by firewall_client.
    assert nodes == ["orchestrator"], f"unexpected rows: {nodes}"


@pytest.mark.asyncio
async def test_firewall_client_creates_round_under_parent(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    inner = MagicMock()
    inner.chat = SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
            usage=SimpleNamespace(
                prompt_tokens=42,
                completion_tokens=7,
                total_tokens=49,
                prompt_tokens_details=SimpleNamespace(cached_tokens=12),
                completion_tokens_details=None,
            ),
            model="gpt-4o-mini",
        ))
    ))
    logger = EventLogger(session_id="t", log_dir=str(tmp_path))
    firewall = FirewallStack(logger=logger)
    client = FirewalledAsyncOpenAI(base=inner, firewall=firewall)

    async with NodeTrace(
        store, chat_id="c", case_id="c", turn_id="t",
        node="specialist.spend_payments", depth=0,
    ) as parent:
        await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    rows = conn.execute(
        "SELECT node, parent_id, depth, prompt_tokens, completion_tokens, "
        "       cached_input_tokens, model "
        "FROM node_trace ORDER BY id"
    ).fetchall()
    assert rows[0][0] == "specialist.spend_payments"
    assert rows[1] == (
        "specialist.spend_payments.round_1",
        parent.row_id, 1, 42, 7, 12, "gpt-4o-mini",
    )


import asyncio


@pytest.mark.asyncio
async def test_firewall_gate_records_queue_wait(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "traces2.db"))
    logger = EventLogger(session_id="t2", log_dir=str(tmp_path))
    firewall = FirewallStack(
        logger=logger, specialist_concurrency=1, orchestrator_concurrency=1,
    )
    # Hold the orchestrator semaphore so gate() has to wait for it.
    await firewall.orchestrator_semaphore.acquire()

    async def waiter_task():
        async with NodeTrace(
            store, chat_id="c", case_id="c", turn_id="t",
            node="orchestrator", depth=0,
        ) as nt:
            async with firewall.gate():
                pass
            return nt.row_id

    task = asyncio.create_task(waiter_task())
    await asyncio.sleep(0.15)
    firewall.orchestrator_semaphore.release()
    row_id = await task

    conn = sqlite3.connect(str(tmp_path / "traces2.db"))
    qw = conn.execute(
        "SELECT queue_wait_ms FROM node_trace WHERE id = ?", (row_id,)
    ).fetchone()[0]
    assert qw is not None and qw >= 100
