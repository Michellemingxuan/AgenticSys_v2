import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from llm.firewall_stack import FirewallStack
from llm.safechain_client import SafeChainAsyncOpenAI, _SafeChainChatCompletions
from logger.event_logger import EventLogger
from tools.node_trace import NodeTrace, NodeTraceStore


@pytest.mark.asyncio
async def test_safechain_create_records_round_with_tiktoken(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    logger = EventLogger(session_id="t", log_dir=str(tmp_path))
    firewall = FirewallStack(logger=logger)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o-mini", firewall=firewall)

    # Stub _invoke to bypass real safechain.
    async def _stub_invoke(self_, *, model, messages, tools, response_format, stream, **kw):
        from llm.safechain_client import _completion_from_message

        # Stands in for the LangChain AIMessage the real transport returns.
        class _Msg:
            content = '{"output":"hi"}'
            tool_calls: list = []
            response_metadata: dict = {}
            id = None

        return _completion_from_message(_Msg(), model)

    with patch.object(_SafeChainChatCompletions, "_invoke", _stub_invoke):
        async with NodeTrace(
            store, chat_id="c", case_id="c", turn_id="t",
            node="specialist.modeling", depth=0,
        ):
            await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "estimate me"}],
            )

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    rows = conn.execute(
        "SELECT node, prompt_tokens, completion_tokens, cost_usd "
        "FROM node_trace ORDER BY id"
    ).fetchall()
    assert rows[0][0] == "specialist.modeling"
    assert rows[1][0] == "specialist.modeling.round_1"
    assert rows[1][1] > 0
    assert rows[1][2] > 0
    # Cost should be a positive float for a known model.
    assert (rows[1][3] or 0) > 0
