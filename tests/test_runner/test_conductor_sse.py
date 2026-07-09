"""Locks the SSE invariant's highest-risk surface: that
``TurnRunner._replay_from_cache()`` (runner/turn/conductor.py) re-emits the
full replay set on a cache hit — team_plan, agent_started, agent_completed,
chart, final (plus agent_message + turn_done) — so a cached-answer replay
never looks like a silent failure to the reviewer (see the module docstring's
"SSE invariant" paragraph).

Drives the REAL ``_replay_from_cache`` against a fake ``sess`` (no mocking of
the method under test). ``_NODE_TRACE_STORE`` is patched to ``None`` so the
run degrades to the documented ``_open_node`` no-op path (tools/node_trace/
core.py: store is None -> _NullNode) instead of writing to the real on-disk
trace DB — a config-constant swap, not a stub of the code under test.
"""
import types

import pytest

import runner.turn.conductor as conductor
from runner.turn.cache import _normalize_q
from runner.turn.conductor import TurnRunner


class _FakeVerdict:
    """Mirrors the subset of chat_agent.screen()'s verdict that
    _replay_from_cache reads: redacted_question (cache-key input) and the
    near-duplicate fields (only consulted on an exact-match miss)."""

    def __init__(self, redacted_question):
        self.redacted_question = redacted_question
        self.near_duplicate_of = None
        self.near_duplicate_reason = None


class _FakeSess:
    def __init__(self, qa_cache):
        self.events = []
        self.qa_cache = qa_cache
        self.specialist_kb = {}
        self.case_id = "case-1"
        self.logger = types.SimpleNamespace(
            log=lambda *a, **k: None, session_id="chat-1",
        )

    def emit(self, event, payload):
        self.events.append(event)


def _seeded_cache():
    """One prior turn's cache entry, shaped exactly like
    runner/turn/cache.py::_store_cached_qa writes it (see conductor.py
    _finalize's _store_cached_qa call): answer/flags/data_pull_request/
    turn_id_origin/origin_question/charts/tool_calls, keyed by
    _normalize_q(verdict.redacted_question). Includes BOTH a tool_calls
    entry and a chart so all five replay-path SSE events fire.
    """
    question = "the cached question"
    key = _normalize_q(question)
    return {
        key: {
            "answer": "The cached answer text.",
            "flags": [],
            "data_pull_request": None,
            "turn_id_origin": "t0",
            "origin_question": question,
            "charts": [
                {
                    "specialist": "collections",
                    "topic": "dpd_trend",
                    "url": "reports/case-1/charts/dpd_trend.png",
                    "claim": "DPD trended up over the last 3 months.",
                    "source_call": "collections_specialist",
                    "kind": "line",
                    "vega_spec": None,
                },
            ],
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "tool": "collections_specialist",
                    "sub_question": "what is the DPD trend?",
                    "payload": '{"result": "ok"}',
                    "duration_ms": 1200,
                },
            ],
            "turn_seq": 1,
        }
    }


@pytest.mark.asyncio
async def test_cache_replay_emits_full_sse_set(monkeypatch):
    monkeypatch.setattr(conductor, "_NODE_TRACE_STORE", None)
    question = "the cached question"
    sess = _FakeSess(_seeded_cache())
    runner = TurnRunner(sess, turn_id="t1", question=question)
    # Set by _screen() in the real flow; mirror it here so the cache-key
    # lookup (_normalize_q(verdict.redacted_question)) matches the seeded
    # entry's key.
    runner.verdict = _FakeVerdict(question)

    replayed = await runner._replay_from_cache()

    assert replayed is True
    # This is the REAL emit order from _replay_from_cache: team_plan, then
    # one agent_started/agent_completed pair per cached tool_call, then one
    # chart per cached chart, then final, agent_message, turn_done. The
    # brief's 5-event list (team_plan/agent_started/agent_completed/chart/
    # final) is a strict prefix of this — agent_message + turn_done follow
    # final on this path, same as every other terminal path in the module.
    assert sess.events == [
        "team_plan", "agent_started", "agent_completed", "chart",
        "final", "agent_message", "turn_done",
    ]
    assert sess.events[:5] == [
        "team_plan", "agent_started", "agent_completed", "chart", "final",
    ]


@pytest.mark.asyncio
async def test_cache_miss_emits_nothing_and_returns_false(monkeypatch):
    monkeypatch.setattr(conductor, "_NODE_TRACE_STORE", None)
    sess = _FakeSess({})
    runner = TurnRunner(sess, turn_id="t2", question="unseen question")
    runner.verdict = _FakeVerdict("unseen question")

    replayed = await runner._replay_from_cache()

    assert replayed is False
    assert sess.events == []
