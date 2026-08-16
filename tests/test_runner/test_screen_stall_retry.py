"""The screen phase abandons a wedged attempt instead of waiting out its budget.

The per-call safechain fence cannot cover this phase: it is 40s against a 30s
phase budget, so the phase fence always fires first and the reviewer gets
"question check took too long" with no second attempt.
"""
import asyncio
import logging

import pytest

from runner.turn import conductor


class _Logger:
    def __init__(self):
        self.events = []

    def log(self, event, payload=None):
        self.events.append((event, payload or {}))

    def kinds(self):
        return [e for e, _ in self.events]


class _ChatAgent:
    """`screen` behaves per attempt: a number sleeps that long, a value returns."""
    def __init__(self, *behaviours):
        self.behaviours = list(behaviours)
        self.calls = 0

    async def screen(self, question, prior_questions=None):
        b = self.behaviours[min(self.calls, len(self.behaviours) - 1)]
        self.calls += 1
        if isinstance(b, (int, float)):
            await asyncio.sleep(b)
            return "slept"
        return b


class _Sess:
    def __init__(self, agent):
        self.chat_agent = agent
        self.logger = _Logger()


@pytest.fixture
def fast_fences(monkeypatch):
    """Real sleeps, tiny fences — the timing is the behaviour under test."""
    monkeypatch.setattr(conductor, "_SCREEN_TIMEOUT_S", 0.30)
    monkeypatch.setattr(conductor, "_SCREEN_STALL_RETRY_S", 0.10)


def _run(sess):
    return asyncio.run(conductor._screen_with_stall_retry(sess, "q", []))


def test_a_healthy_screen_runs_once_and_is_not_tagged(fast_fences):
    sess = _Sess(_ChatAgent("verdict"))
    assert _run(sess) == "verdict"
    assert sess.chat_agent.calls == 1
    assert "screen_stalled" not in sess.logger.kinds()


def test_a_stalled_attempt_is_abandoned_and_re_issued(fast_fences):
    # First attempt outlasts the 0.1s fence; the second returns immediately.
    sess = _Sess(_ChatAgent(5.0, "verdict"))
    assert _run(sess) == "verdict"
    assert sess.chat_agent.calls == 2
    assert "screen_stalled" in sess.logger.kinds()
    assert "screen_retry_stalled" not in sess.logger.kinds()


def test_the_retry_shares_the_phase_budget_rather_than_extending_it(fast_fences):
    """Worst case must stay at _SCREEN_TIMEOUT_S so no caller is re-tuned."""
    sess = _Sess(_ChatAgent(5.0, 5.0))
    loop_t0 = asyncio.get_event_loop_policy().new_event_loop()
    try:
        t0 = loop_t0.time()
        with pytest.raises(asyncio.TimeoutError):
            loop_t0.run_until_complete(
                conductor._screen_with_stall_retry(sess, "q", []))
        elapsed = loop_t0.time() - t0
    finally:
        loop_t0.close()
    assert sess.chat_agent.calls == 2
    # 0.30s total, not 0.10 + 0.30.
    assert elapsed == pytest.approx(0.30, abs=0.15)
    assert "screen_retry_stalled" in sess.logger.kinds()


def test_both_stalling_raises_timeout_for_the_existing_handler(fast_fences):
    """The phase handler upstream keys off TimeoutError to emit screen_timeout."""
    sess = _Sess(_ChatAgent(5.0, 5.0))
    with pytest.raises(asyncio.TimeoutError):
        _run(sess)


def test_zero_disables_the_retry(monkeypatch):
    monkeypatch.setattr(conductor, "_SCREEN_TIMEOUT_S", 0.20)
    monkeypatch.setattr(conductor, "_SCREEN_STALL_RETRY_S", 0)
    sess = _Sess(_ChatAgent(5.0, "verdict"))
    with pytest.raises(asyncio.TimeoutError):
        _run(sess)
    assert sess.chat_agent.calls == 1          # one attempt, full budget
    assert "screen_stalled" not in sess.logger.kinds()


def test_a_fence_wider_than_the_budget_is_clamped(monkeypatch):
    """Otherwise the first attempt outlives the phase it is bounded by — the
    same bug that was fixed in the safechain fence."""
    monkeypatch.setattr(conductor, "_SCREEN_TIMEOUT_S", 0.10)
    monkeypatch.setattr(conductor, "_SCREEN_STALL_RETRY_S", 99)
    sess = _Sess(_ChatAgent(5.0, "verdict"))
    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            loop.run_until_complete(
                conductor._screen_with_stall_retry(sess, "q", []))
        elapsed = loop.time() - t0
    finally:
        loop.close()
    assert elapsed < 1.0


def test_telemetry_failure_cannot_break_the_phase(monkeypatch, fast_fences):
    monkeypatch.setattr(conductor, "attach_tag",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sess = _Sess(_ChatAgent(5.0, "verdict"))
    assert _run(sess) == "verdict"
