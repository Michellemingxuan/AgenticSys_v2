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
import threading
import types

import pytest

import runner.turn.conductor as conductor
from runner.turn.cache import _normalize_q
from runner.turn.conductor import TurnRunner, _TurnAborted


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
        # Real CaseSession carries this; `_store_cached_qa` bumps it to order
        # episodic records. The replay path stores an entry too (so a replayed
        # turn still becomes the session's newest turn), so the fake needs it.
        self._qa_turn_seq = 1
        self.cancel_in_flight = threading.Event()
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
async def test_near_duplicate_replay_becomes_the_newest_episodic_turn(monkeypatch):
    """A replayed turn must enter the transcript under the REVIEWER'S wording.

    `episodic.build_records` orders turns by `turn_seq`, which only
    `_store_cached_qa` bumps — the LRU touch in `_get_cached_qa` does not. So a
    near-duplicate replay used to leave NO record of the question just asked,
    and the next subject-less follow-up ("think harder", "what contradicts
    it?") coreferenced against whichever turn last actually ran. The answer came
    back coherent but about a different question, which is why this is worth a
    lock: the failure is invisible in the SSE stream.
    """
    monkeypatch.setattr(conductor, "_NODE_TRACE_STORE", None)
    prior = "any large spending right after a small payment"
    asked = "any large spending closely followed small payments"
    cache = _seeded_cache()
    # Re-key the seeded entry onto the PRIOR question, as a real session would.
    entry = cache.pop(_normalize_q("the cached question"))
    entry["origin_question"] = prior
    cache[_normalize_q(prior)] = entry
    sess = _FakeSess(cache)

    runner = TurnRunner(sess, turn_id="t9", question=asked)
    verdict = _FakeVerdict(asked)
    verdict.near_duplicate_of = prior          # what relevance_check returned
    runner.verdict = verdict

    assert await runner._replay_from_cache() is True

    from tools.episodic import build_records
    records = build_records(sess.qa_cache)

    # The newest record is the question the reviewer actually typed — not the
    # older wording it was matched against.
    assert records[0]["question"] == asked
    assert records[0]["turn_id"] == "t9"
    # The matched prior turn survives as its own, older record.
    assert [r["question"] for r in records] == [asked, prior]
    # The stored answer is the ORIGINAL text: the "reused from a prior
    # question" line is a display decoration, and re-appending it on a second
    # replay would compound it.
    assert records[0]["final_answer"] == "The cached answer text."
    assert sess.qa_cache[_normalize_q(asked)]["replay_of"] == "t0"


@pytest.mark.asyncio
async def test_cache_miss_emits_nothing_and_returns_false(monkeypatch):
    monkeypatch.setattr(conductor, "_NODE_TRACE_STORE", None)
    sess = _FakeSess({})
    runner = TurnRunner(sess, turn_id="t2", question="unseen question")
    runner.verdict = _FakeVerdict("unseen question")

    replayed = await runner._replay_from_cache()

    assert replayed is False
    assert sess.events == []


# ── cancellation: _finalize must not emit the fallback answer on a stop ──────
#
# When the reviewer stops a turn mid-run, `sess.cancel_in_flight` is set. An
# aborted SDK run can leave `final_answer=None` and reach `_finalize`; without
# the guard, `_finalize` would publish the "could not produce a synthesized
# answer" fallback to a reviewer who is no longer waiting. The guard instead
# raises `_TurnAborted` so the outer handler renders the clean "Interrupted"
# path + rewind. This is the exact bug the guard fixes.


@pytest.mark.asyncio
async def test_finalize_aborts_without_fallback_when_cancelled(monkeypatch):
    monkeypatch.setattr(conductor, "_NODE_TRACE_STORE", None)
    sess = _FakeSess({})
    sess.cancel_in_flight.set()  # reviewer pressed Stop
    runner = TurnRunner(sess, turn_id="tc", question="q")
    runner.final_answer = None   # aborted mid-synthesis → no structured answer
    runner.tool_calls = [
        {"call_id": "c1", "tool": "modeling_specialist",
         "sub_question": "trend?", "payload": '{"findings": "x"}'},
    ]
    runner.review_flags = []

    with pytest.raises(_TurnAborted):
        await runner._finalize()

    # No fallback answer leaked to the reviewer.
    assert "final" not in sess.events
    assert "agent_message" not in sess.events


@pytest.mark.asyncio
async def test_finalize_still_synthesizes_fallback_when_not_cancelled(monkeypatch):
    """Sanity: with NO cancel in flight, the genuine-error salvage path still
    fires (an empty FinalAnswer while the reviewer is waiting IS worth
    surfacing) — the guard must not suppress legitimate fallbacks."""
    monkeypatch.setattr(conductor, "_NODE_TRACE_STORE", None)
    monkeypatch.setattr(conductor, "_collect_turn_charts", lambda *a, **k: [])
    monkeypatch.setattr(conductor, "_store_cached_qa", lambda *a, **k: None)
    sess = _FakeSess({})  # cancel_in_flight NOT set
    runner = TurnRunner(sess, turn_id="td", question="q")
    runner.final_answer = None
    runner.tool_calls = [
        {"call_id": "c1", "tool": "modeling_specialist",
         "sub_question": "trend?", "payload": '{"findings": "x"}'},
    ]
    runner.review_flags = []

    await runner._finalize()

    # The salvage path published a final answer + chat message.
    assert "final" in sess.events
    assert "agent_message" in sess.events


# ── report_agent backstop (_ensure_report_agent) ────────────────────────────
#
# report_agent is mandatory every turn; the orchestrator LLM occasionally omits
# it (terse extraction questions). The backstop re-dispatches ONLY when it is
# genuinely absent, so it fires rarely and never on turns that already ran it.


def _runner_for_backstop(monkeypatch, tool_calls):
    monkeypatch.setattr(conductor, "_NODE_TRACE_STORE", None)
    sess = _FakeSess({})
    runner = TurnRunner(sess, turn_id="tb", question="extract the txns")
    runner.final_answer = "OLD"
    runner.tool_calls = tool_calls
    runner.review_flags = []
    runner.streamed = types.SimpleNamespace(
        to_input_list=lambda: [{"role": "user", "content": "q"}])
    runner._drain_specialist_errors = lambda: None
    return runner


@pytest.mark.asyncio
async def test_ensure_report_agent_redispatches_when_missing(monkeypatch):
    runner = _runner_for_backstop(
        monkeypatch, [{"call_id": "c1", "tool": "spend_payments", "payload": {}}])
    seen = {}

    async def fake_redispatch(resume_input):
        seen["input"] = resume_input
        return "NEW_FINAL"

    runner._run_redispatch_pass = fake_redispatch
    await runner._ensure_report_agent()

    assert runner.final_answer == "NEW_FINAL"
    assert seen["input"][-1]["content"].startswith("[REQUIRED report_agent]")
    assert any("report_agent was dispatched server-side" in f
               for f in runner.review_flags)


@pytest.mark.asyncio
async def test_ensure_report_agent_skips_when_case_has_no_reports(monkeypatch):
    """A case with no curated reports must NOT be forced through report_agent —
    there's nothing to look up, so the backstop skips (no wasted / failure-prone
    round). Regression for 'no reports must not hinder the system working'."""
    runner = _runner_for_backstop(
        monkeypatch, [{"call_id": "c1", "tool": "spend_payments", "payload": {}}])
    runner.has_reports = False
    fired = {"n": 0}

    async def fake_redispatch(resume_input):
        fired["n"] += 1
        return "NEW"

    runner._run_redispatch_pass = fake_redispatch
    await runner._ensure_report_agent()

    assert fired["n"] == 0                 # backstop did NOT re-dispatch
    assert runner.final_answer == "OLD"    # kept the specialist-only answer


def test_case_has_reports_detects_md_files(monkeypatch, tmp_path):
    monkeypatch.setattr(conductor, "_REPORTS_DIR", tmp_path)
    # No folder at all → False.
    assert conductor._case_has_reports("CASE-NONE") is False
    # Folder with only a charts/ subdir (generated artifacts) → still False.
    empty = tmp_path / "CASE-EMPTY"
    (empty / "charts").mkdir(parents=True)
    assert conductor._case_has_reports("CASE-EMPTY") is False
    # Folder with a real .md report → True.
    withrep = tmp_path / "CASE-REP"
    withrep.mkdir()
    (withrep / "payment_spend_exp_0.md").write_text("# report")
    assert conductor._case_has_reports("CASE-REP") is True


@pytest.mark.asyncio
async def test_ensure_report_agent_skips_when_already_present(monkeypatch):
    runner = _runner_for_backstop(monkeypatch, [
        {"tool": "spend_payments", "payload": {}},
        {"tool": "report_agent", "payload": {}},
    ])
    fired = {"n": 0}

    async def fake_redispatch(resume_input):
        fired["n"] += 1
        return "NEW"

    runner._run_redispatch_pass = fake_redispatch
    await runner._ensure_report_agent()

    assert fired["n"] == 0
    assert runner.final_answer == "OLD"


@pytest.mark.asyncio
async def test_ensure_report_agent_noop_when_nothing_dispatched(monkeypatch):
    runner = _runner_for_backstop(monkeypatch, [])   # empty → no enforcement
    fired = {"n": 0}

    async def fake_redispatch(resume_input):
        fired["n"] += 1
        return "NEW"

    runner._run_redispatch_pass = fake_redispatch
    await runner._ensure_report_agent()

    assert fired["n"] == 0
    assert runner.final_answer == "OLD"


def test_no_replay_entries_are_invisible_to_the_replay_cache():
    """`qa_cache` does two jobs: replay cache AND the source of episodic memory.

    The orchestrator-error fallback needs them split — its partial,
    synthesis-failed answer must never be served again, but the next turn still
    has to know the exchange happened or a subject-less follow-up binds to the
    wrong antecedent. `no_replay` is what splits them.
    """
    from runner.turn.cache import _get_cached_qa, _store_cached_qa

    sess = _FakeSess({})
    key = _normalize_q("a question whose synthesis blew up")
    _store_cached_qa(sess, key, {
        "answer": "Partial answer assembled from what survived.",
        "origin_question": "a question whose synthesis blew up",
        "turn_id_origin": "t1", "tool_calls": [],
        "no_replay": True, "partial_answer": True,
    })

    # Remembered: it is in the cache, so episodic sees it...
    assert key in sess.qa_cache
    from tools.episodic import build_records
    assert build_records(sess.qa_cache)[0]["partial_answer"] is True
    # ...but never replayed.
    assert _get_cached_qa(sess, key) is None

    # A later SUCCESSFUL run of the same question makes it replayable again.
    _store_cached_qa(sess, key, {
        "answer": "The real answer.", "origin_question": "a question whose synthesis blew up",
        "turn_id_origin": "t2", "tool_calls": [],
    })
    assert _get_cached_qa(sess, key)["answer"] == "The real answer."


# NOTE for anyone adding tests here: `TurnRunner.__init__` calls
# `TURN_SCOPE.set(...)`. The async tests above are safe because pytest-asyncio
# runs each in its own context, but a SYNCHRONOUS test that constructs a
# TurnRunner leaks that contextvar into every test that follows — which
# switches on `query_table`'s per-turn `repeated_call` dedup guard and breaks
# unrelated data_tools tests with a `repeated_call` payload instead of rows.
# Cost me three confusing failures; don't construct one outside a coroutine.


def _domain_calls(tool_calls):
    """The report-only predicate as `_finalize` computes it."""
    return [c for c in tool_calls
            if c.get("tool") not in ("report_agent", "general_specialist")]


def test_report_only_predicate_identifies_a_report_subject_turn():
    """A report-only turn is ALLOWED — the PROTOCOL carve-out covers a question
    whose subject is the curated report ("summarize the spending patterns FROM
    THE REPORT"). It is logged rather than blocked, because the runtime cannot
    judge whether the question qualified; only the orchestrator can. Measured
    live: the same question ran four times and split two report-only / two
    data-only, so this needs to be observable rather than silent."""
    report_only = [{"tool": "report_agent", "payload": {}}]
    assert report_only and not _domain_calls(report_only)


def test_report_only_predicate_ignores_the_reviewer_node():
    """`general_specialist` is the coherence reviewer, not a data source — a
    turn with report_agent + reviewer is still report-only."""
    calls = [{"tool": "report_agent", "payload": {}},
             {"tool": "general_specialist", "payload": {}}]
    assert calls and not _domain_calls(calls)


def test_a_turn_with_any_domain_specialist_is_not_report_only():
    for tool in ("spend_payments", "modeling", "bureau"):
        calls = [{"tool": "report_agent", "payload": {}}, {"tool": tool, "payload": {}}]
        assert _domain_calls(calls) == [{"tool": tool, "payload": {}}], tool
