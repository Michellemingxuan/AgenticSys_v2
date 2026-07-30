"""Per-specialist durations in the reasoning trace must be per-SPECIALIST.

The SDK gathers parallel tool calls and only queues their output items after
the SLOWEST one returns (agents/_run_impl.py: `results = await asyncio.gather(
*tasks)` then `stream_step_items_to_queue`). Every parallel sibling therefore
shares a start AND an end stream timestamp, so a stream-derived duration
reports the BATCH, not the specialist — observed in the UI as crossbu and
spend_payments both showing 8.42s while node_trace had them differing.

So agent_tool publishes its own measured wall-clock and the SSE layer prefers
it. These tests pin both halves.
"""
import json
import types
from unittest.mock import AsyncMock, patch

import pytest
from agents import Agent, RunContextWrapper

from agent_factories.agent_tools.agent_tool import agent_tool
from models.types import SpecialistOutput
from runner.turn.sse import map_run_item


class _Sess:
    def __init__(self):
        self.emitted = []
        self.logger = types.SimpleNamespace(log=lambda *a, **k: None,
                                            session_id="s")

    def emit(self, event, payload):
        self.emitted.append((event, payload))


def _output_item(call_id, output="{}"):
    """A REAL ToolCallOutputItem — map_run_item dispatches on isinstance, so a
    duck-typed stand-in is silently ignored and the test passes vacuously."""
    from agents.items import ToolCallOutputItem

    return ToolCallOutputItem(
        agent=Agent(name="x", instructions="x", tools=[]),
        raw_item={"call_id": call_id, "output": output,
                  "type": "function_call_output"},
        output=output,
    )


def _emit_output(call_id, tool, *, run_ms_by_tool=None, started_ms=0):
    sess = _Sess()
    tool_calls = [{"call_id": call_id, "tool": tool, "sub_question": "q"}]
    map_run_item(
        _output_item(call_id), sess=sess, turn_id="t", orch_t0=0.0,
        tool_calls=tool_calls, call_index_by_id={call_id: 0},
        started_at_by_call={call_id: started_ms},
        team_plan_emitted=True, first_tool_call_logged=True,
        safe_dump=lambda x: x, drain_specialist_errors=lambda: None,
        run_ms_by_tool=run_ms_by_tool,
    )
    completed = [p for e, p in sess.emitted if e == "agent_completed"]
    return completed[0] if completed else None


# ── agent_tool publishes what it measured ───────────────────────────────────

def _ctx():
    return types.SimpleNamespace(
        logger=types.SimpleNamespace(log=lambda *a, **k: None),
        _specialist_histories={}, _specialist_errors={},
        _specialist_turn_records={}, _degraded_specialists={},
        _pending_distillers=[], _specialist_run_ms={},
    )


class _Result:
    final_output = SpecialistOutput(domain="d", mode="chat", findings="f")

    def to_input_list(self):
        return []


@pytest.mark.asyncio
async def test_agent_tool_publishes_its_measured_duration():
    ctx = _ctx()
    ctx._specialist_errors = []
    with patch("agent_factories.agent_tools.agent_tool.Runner.run",
               new=AsyncMock(return_value=_Result())):
        wrapped = agent_tool(Agent(name="i", instructions="x", tools=[]),
                             name="crossbu", description="d")
        await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "q"}))

    assert list(ctx._specialist_run_ms) == ["crossbu"]
    assert len(ctx._specialist_run_ms["crossbu"]) == 1
    assert isinstance(ctx._specialist_run_ms["crossbu"][0], int)
    for t in ctx._pending_distillers:
        t.cancel()


@pytest.mark.asyncio
async def test_a_failed_run_also_publishes_a_duration():
    """Every exit path goes through timer.summary(), so a failure is timed too
    — otherwise the UI would fall back to batch timing for exactly the runs a
    reviewer most wants to understand."""
    ctx = _ctx()
    ctx._specialist_errors = []
    with patch("agent_factories.agent_tools.agent_tool.Runner.run",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        wrapped = agent_tool(Agent(name="i", instructions="x", tools=[]),
                             name="bureau", description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "q"}))

    assert out.startswith("[FAILED bureau]")
    assert ctx._specialist_run_ms["bureau"]


# ── the SSE layer prefers the measured value ────────────────────────────────

def test_measured_duration_wins_over_stream_timing():
    payload = _emit_output("c1", "crossbu",
                           run_ms_by_tool={"crossbu": [3120]}, started_ms=0)
    assert payload["duration_ms"] == 3120


def test_parallel_siblings_get_their_own_durations():
    """The reported symptom: both showed the batch time. They must differ."""
    runs = {"crossbu": [3120], "spend_payments": [8420]}
    a = _emit_output("c1", "crossbu", run_ms_by_tool=runs)
    b = _emit_output("c2", "spend_payments", run_ms_by_tool=runs)
    assert (a["duration_ms"], b["duration_ms"]) == (3120, 8420)


def test_falls_back_to_stream_timing_when_nothing_was_published():
    """Non-specialist tools never publish; they must still get a duration."""
    payload = _emit_output("c1", "some_tool", run_ms_by_tool={})
    assert payload["duration_ms"] >= 0


def test_repeat_calls_to_one_specialist_are_consumed_in_order():
    """Review re-dispatch can run a specialist twice in a turn."""
    runs = {"bureau": [1000, 2000]}
    first = _emit_output("c1", "bureau", run_ms_by_tool=runs)
    second = _emit_output("c2", "bureau", run_ms_by_tool=runs)
    assert (first["duration_ms"], second["duration_ms"]) == (1000, 2000)
    assert runs["bureau"] == []


def test_one_specialists_timings_never_leak_to_another():
    runs = {"crossbu": [3120]}
    other = _emit_output("c9", "spend_payments", run_ms_by_tool=runs)
    assert other["duration_ms"] != 3120
    assert runs["crossbu"] == [3120], "must not be consumed by another tool"


# ── scope provenance belongs in the trace, NOT in the answer ────────────────


def _render(payload: dict) -> str:
    """The suite's specialist-payload → markdown renderer, loaded by path."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "notebooks" / "run_question_suite.py"
    spec = importlib.util.spec_from_file_location("_rqs_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._render_specialist_output(payload)


def test_trace_shows_scope_before_the_per_call_detail():
    out = _render({
        "findings": "Top merchant is 10.0% of spend.",
        "scope": "spends: all dates; model_scores_transaction: 2025-05-01..2025-05-31",
        "measured_over": ["aggregate_column(spends.amount, op=share)"],
    })
    assert "**Scope:** spends: all dates" in out
    assert out.index("**Scope:**") < out.index("**Measured over:**"), \
        "the one-line scope leads; per-call detail follows"


def test_trace_states_an_absent_filter_as_all_dates():
    """The load-bearing half: an unconstrained table must be NAMED, not omitted.

    In `measured_over` an unfiltered call merely lacks a `where` clause, and
    missing text is what a reviewer skims past.
    """
    assert "all dates" in _render({"findings": "f", "scope": "spends: all dates"})


def test_trace_omits_scope_when_no_data_calls_were_made():
    out = _render({"findings": "from the curated report"})
    assert "**Scope:**" not in out


def test_final_answer_never_carries_a_scope_footnote():
    """Drift guard: scope is trace-only.

    It previously appended `_Scope: …` to `answer_text`, which also pushed it
    into the chat message, the qa_cache entry and the Amem write. Bind that
    removal so it cannot creep back via any of those paths.
    """
    import inspect

    from runner.turn import conductor

    assert not hasattr(conductor, "_scope_footnote")
    assert "_Scope:" not in inspect.getsource(conductor)
