"""A specialist that answers from a FAILED tool call is retried once, and
quarantined if the retry doesn't fix it.

The hard-failure path (`[FAILED ...]`) is covered in test_agent_tool.py. This
file covers the SILENT one: `Runner.run` returns a well-formed SpecialistOutput
whose numbers came from a tool call that errored. Left alone, that answer flows
into the KB, Amem and next turn's episodic context, so one broken call poisons
every later turn — see agent_factories/agent_tools/grounding.py.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agents import Agent, RunContextWrapper

from agent_factories.agent_tools.agent_tool import (
    _DEGRADED_RECOVERY,
    _degraded_directive,
    agent_tool,
)
from agent_factories.agent_tools.grounding import classify_tool_output
from models.types import SpecialistOutput


class _Logger:
    def __init__(self):
        self.events = []

    def log(self, evt, payload):
        self.events.append((evt, payload))

    def kinds(self):
        return [e for e, _ in self.events]


def _ctx():
    """AppContext stand-in exposing every channel the quarantine must fence."""
    return SimpleNamespace(
        logger=_Logger(),
        _specialist_histories={},
        _specialist_errors=[],
        _specialist_turn_records={},
        _degraded_specialists={},
        _pending_distillers=[],
    )


class _Result:
    """RunResult stand-in: a transcript plus a final SpecialistOutput.

    `final_output` is the REAL pydantic model, not a namespace — domain
    specialists run with `output_type=SpecialistOutput`, so `redact_payload`
    returns a model rather than a string, and a str-shaped fixture would let a
    str-only banner bug pass the tests.
    """

    def __init__(self, items, findings="Spend rose 2.7x to $41,208."):
        self._items = items
        self.final_output = SpecialistOutput(
            domain="modeling", mode="chat", findings=findings)

    def to_input_list(self):
        return list(self._items)


def _call(call_id, name):
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": "{}"}


def _out(call_id, text):
    return {"type": "function_call_output", "call_id": call_id, "output": text}


# The real marker emitted by data_tools when specs_json won't parse. The
# drift-guard test (tests/test_tools/test_data_tools_error_markers.py) is what
# keeps this string bound to the source; here we only need one known-bad output.
_BROKEN = ("batch_summarize_trend received a specs_json that was malformed: "
           "'['. batch_summarize_trend did NOT run — you have NO data from it.")
_CLEAN = '{"trend": [{"period": "2024-01", "value": 41208.0}]}'


def _degraded_transcript():
    return [_call("c1", "batch_summarize_trend"), _out("c1", _BROKEN)]


def _clean_transcript():
    return [_call("c2", "batch_summarize_trend"), _out("c2", _CLEAN)]


async def _run(ctx, results, name="modeling"):
    """Drive the wrapper with a scripted sequence of Runner.run results."""
    seq = list(results)
    calls = []

    async def _fake_run(agent, run_input, **kw):
        calls.append(run_input)
        return seq.pop(0)

    with patch("agent_factories.agent_tools.agent_tool.Runner.run", new=_fake_run):
        wrapped = agent_tool(Agent(name="inner", instructions="x", tools=[]),
                             name=name, description="d")
        out = await wrapped.on_invoke_tool(
            RunContextWrapper(ctx), json.dumps({"sub_question": "spend trend?"}))
    return out, calls


# ── the retry ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ungrounded_run_is_retried_with_a_directive():
    ctx = _ctx()
    out, calls = await _run(ctx, [
        _Result(_degraded_transcript()),
        _Result(_clean_transcript()),
    ])

    assert len(calls) == 2, "a run built on a failed tool call must be retried"
    # The retry continues from the failed transcript and appends the directive.
    retry_input = calls[1]
    assert isinstance(retry_input, list)
    directive = retry_input[-1]["content"]
    assert "GROUNDING CHECK" in directive
    assert "batch_summarize_trend failed (specs_unparseable)" in directive
    # The recovered answer is clean: no banner, and it is NOT quarantined.
    assert "[DEGRADED" not in out
    assert ctx._degraded_specialists == {}
    assert "specialist_ungrounded_retry" in ctx.logger.kinds()


@pytest.mark.asyncio
async def test_grounded_run_is_not_retried():
    ctx = _ctx()
    out, calls = await _run(ctx, [_Result(_clean_transcript())])

    assert len(calls) == 1
    assert "[DEGRADED" not in out
    assert ctx._degraded_specialists == {}
    assert "specialist_ungrounded_retry" not in ctx.logger.kinds()


@pytest.mark.asyncio
async def test_retry_is_spent_only_once():
    """Two bad runs must not become three — the 2-attempt budget is shared."""
    ctx = _ctx()
    _, calls = await _run(ctx, [
        _Result(_degraded_transcript()),
        _Result(_degraded_transcript()),
    ])
    assert len(calls) == 2


# ── the quarantine ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_still_degraded_run_is_banner_flagged_and_quarantined():
    ctx = _ctx()
    out, _ = await _run(ctx, [
        _Result(_degraded_transcript()),
        _Result(_degraded_transcript()),
    ])

    # The orchestrator still receives the answer, but explicitly marked.
    assert out.startswith("[DEGRADED modeling")
    assert "batch_summarize_trend (specs_unparseable)" in out
    assert "UNSUPPORTED" in out

    # Registered for the conductor to project onto the qa_cache.
    assert list(ctx._degraded_specialists) == ["modeling"]
    assert ctx._degraded_specialists["modeling"][0]["reason"] == "specs_unparseable"
    assert "specialist_ungrounded" in ctx.logger.kinds()


@pytest.mark.asyncio
async def test_degraded_run_reaches_no_cross_turn_channel():
    """The whole point: nothing durable is written for an ungrounded answer."""
    ctx = _ctx()
    await _run(ctx, [
        _Result(_degraded_transcript()),
        _Result(_degraded_transcript()),
    ])

    assert ctx._specialist_turn_records == {}, "must not reach Amem"
    assert ctx._pending_distillers == [], "must not reach the KB or auto-chart"


@pytest.mark.asyncio
async def test_grounded_run_still_writes_its_cross_turn_records():
    """Guard against the quarantine over-firing and starving the happy path."""
    ctx = _ctx()
    await _run(ctx, [_Result(_clean_transcript())])

    assert list(ctx._specialist_turn_records) == ["modeling"]
    assert ctx._pending_distillers, "the distiller must still be scheduled"
    for task in ctx._pending_distillers:
        task.cancel()


@pytest.mark.asyncio
async def test_degraded_answer_is_not_served_from_the_dedup_cache():
    """A repeat sub-question this turn must re-run, not replay the bad answer."""
    ctx = _ctx()
    await _run(ctx, [
        _Result(_degraded_transcript()),
        _Result(_degraded_transcript()),
    ])
    assert not getattr(ctx, "_specialist_call_cache", {}), \
        "a degraded payload must never be cached"


# ── the directive ───────────────────────────────────────────────────────────

def test_directive_covers_every_reason_grounding_can_return():
    """Drift guard: a new classify_tool_output reason needs recovery guidance,
    otherwise the retry gets a generic nudge and usually repeats the mistake."""
    import inspect

    from agent_factories.agent_tools import grounding

    src = inspect.getsource(grounding.classify_tool_output)
    reasons = {
        literal
        for literal in ("specs_unparseable", "no_groups", "no_buckets",
                        "table_not_found", "data_layer_uninitialized",
                        "spec_rejected")
        if f'"{literal}"' in src
    }
    assert reasons, "failed to read reasons out of classify_tool_output"
    assert reasons <= set(_DEGRADED_RECOVERY), (
        f"no recovery guidance for: {sorted(reasons - set(_DEGRADED_RECOVERY))}")


def test_directive_names_every_broken_tool():
    errors = [
        {"tool": "summarize_trend", "reason": "no_buckets"},
        {"tool": "query_table", "reason": "table_not_found"},
    ]
    text = _degraded_directive(errors)
    assert "summarize_trend failed (no_buckets)" in text
    assert "query_table failed (table_not_found)" in text
    # The escape hatch must be explicit, or the retry invents numbers instead.
    assert "data gap" in text


def test_broken_marker_still_classifies():
    """Cheap canary: if data_tools stops emitting this text, the fixtures above
    are testing nothing. The full drift guard lives in test_data_tools_error_markers."""
    assert classify_tool_output("batch_summarize_trend", _BROKEN) == "specs_unparseable"
