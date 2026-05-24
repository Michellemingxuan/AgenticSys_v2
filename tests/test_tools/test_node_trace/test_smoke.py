"""End-to-end smoke: one ChatAgent.screen() call populates chat.* trace rows.

This is the minimal slice of the real production flow that exercises:
  - TURN_SCOPE contextvar resolution inside _open_node
  - NodeTrace insert + update lifecycle
  - The depth-0 chat.* wrappers in chat_agent.py
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_factories.chat_agent import ChatAgent
from logger.event_logger import EventLogger
from models.types import LLMResult
from tools.node_trace import NodeTraceStore, TURN_SCOPE, TurnScope


@pytest.mark.asyncio
async def test_screen_populates_chat_nodes(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "smoke.db"))
    logger = EventLogger(session_id="case-X-smoke", log_dir=str(tmp_path))

    # Stub LLM shim: both screen calls (redact + relevance_check) get
    # canned JSON responses that fail-open / pass the verdict.
    class _StubLLM:
        async def ainvoke(self, *, system_prompt, user_message, **kw):
            if "Text to redact" in user_message:
                return LLMResult(
                    status="success",
                    data={"redacted": "what's the case status", "masked_spans": []},
                )
            # relevance_check
            return LLMResult(
                status="success",
                data={"passed": True, "reason": "",
                      "near_duplicate_of": "", "near_duplicate_reason": ""},
            )

    chat = ChatAgent(
        _StubLLM(), logger,
        pillar_config={"concept_glossary": ""},
        node_trace_store=store,
    )

    # Match what server.py does at turn start.
    TURN_SCOPE.set(TurnScope(
        chat_id=logger.session_id,
        case_id="X",
        turn_id="T-smoke-001",
    ))

    # Use a long-ish, digit-bearing question so redact actually runs
    # (otherwise the trivial-no-PII fast path skips that LLM call).
    verdict = await chat.screen(
        "what's the case status for account 1234567890?",
        prior_questions=[],
    )
    assert verdict.passed is True

    conn = sqlite3.connect(str(tmp_path / "smoke.db"))
    nodes = [r[0] for r in conn.execute(
        "SELECT node FROM node_trace ORDER BY id"
    ).fetchall()]
    # Both chat.redact and chat.relevance_check should be present.
    assert "chat.redact" in nodes
    assert "chat.relevance_check" in nodes
