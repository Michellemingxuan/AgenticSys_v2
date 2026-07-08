"""Tests for tools.distiller_pass: _distill_and_persist standalone behavior.

Moved from tests/test_agent_factories/test_redacting_tool.py as part of the
redacting_tool decomposition (Task 2 of 5).
"""
import asyncio

from tools.distiller_pass import _distill_and_persist


def test_distill_and_persist_noop_when_distiller_unwired():
    """Tests / legacy paths without _distiller or _specialist_kb must behave
    like the legacy single-turn flow — no errors, no KB updates."""
    from types import SimpleNamespace

    ctx_no_distiller = SimpleNamespace(
        logger=None, _specialist_kb={}, _distiller=None, _turn_id=None,
    )
    n = asyncio.run(
        _distill_and_persist(ctx_no_distiller, "x", "q", "out")
    )
    assert n == 0
    assert ctx_no_distiller._specialist_kb == {}


def test_distill_and_persist_skips_report_agent():
    """report_agent returns ReportDraft (narrative), not SpecialistOutput —
    running the distiller on it costs ~20s for trivial KPs. The wrapper
    must short-circuit so neither the distiller LLM call nor the KB write
    happens for report_agent. The distiller mock is asserted untouched."""
    from types import SimpleNamespace

    distiller_calls: list = []

    class _MockDistiller:
        def __call__(self, *args, **kwargs):
            distiller_calls.append((args, kwargs))
            raise AssertionError("distiller should not have been invoked for report_agent")

    logged_events: list = []

    class _MockLogger:
        def log(self, event, payload):
            logged_events.append((event, payload))

    ctx = SimpleNamespace(
        logger=_MockLogger(),
        _specialist_kb={},
        _distiller=_MockDistiller(),
        _turn_id="t-1",
    )
    n = asyncio.run(
        _distill_and_persist(ctx, "report_agent", "q", "out")
    )
    assert n == 0
    assert distiller_calls == []
    assert ctx._specialist_kb == {}
    # The skip is observable in the JSONL so perf regressions are auditable.
    assert any(e == "distiller_skipped" and p.get("specialist") == "report_agent"
               for e, p in logged_events)
