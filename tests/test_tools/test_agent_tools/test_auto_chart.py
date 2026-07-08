"""Tests for tools.auto_chart: auto-chart renderer from specialist tool outputs."""
import asyncio
import json
import pytest


@pytest.mark.asyncio
async def test_auto_chart_emits_chart_pending_before_render(tmp_path):
    """The auto-chart path renders charts from specialist tool outputs
    WITHOUT the specialist calling make_chart (this is what actually
    happens in practice — the JSONL log shows viz_rendered/auto_chart_rendered
    and no make_chart). It must emit a `chart_pending` SSE event per chart,
    keyed by (specialist, topic) so it matches the eventual end-of-turn
    `chart` event and the frontend clears the "working on the plot…"
    placeholder. (make_chart already does this; the auto-chart path didn't.)
    """
    from types import SimpleNamespace
    from tools.agent_tools.auto_chart import _auto_chart_from_tool_outputs

    class _Logger:
        def __init__(self):
            self.events = []

        def log(self, evt, payload):
            self.events.append((evt, payload))

    emits: list = []
    case_folder = tmp_path / "CASE-AUTOCHART"
    case_folder.mkdir()
    ctx = SimpleNamespace(
        logger=_Logger(),
        case_folder=case_folder,
        _specialist_kb={},
        _turn_id="t-auto",
        _catalog=None,
        _emit_event=lambda evt, payload: emits.append((evt, payload)),
    )

    # A summarize_trend-shaped tool output with ≥4 points so a chart renders.
    tool_outputs = json.dumps({
        "series": [
            {"period": "2024-11", "raw_value": 720},
            {"period": "2024-12", "raw_value": 705},
            {"period": "2025-01", "raw_value": 690},
            {"period": "2025-02", "raw_value": 680},
        ],
        "value_column": "fico",
        "table": "model_scores",
    })

    n = await _auto_chart_from_tool_outputs(ctx, "modeling", tool_outputs)
    assert n >= 1, "auto-chart should have rendered at least one chart"

    # A KP was persisted; grab its topic — the pending event must match it.
    assert ctx._specialist_kb.get("modeling"), "auto-chart should persist a KP"
    topic = ctx._specialist_kb["modeling"][0]["topic"]

    pending = [p for (e, p) in emits if e == "chart_pending"]
    assert pending, f"auto-chart emitted no chart_pending; events={emits}"
    assert any(
        p.get("specialist") == "modeling" and p.get("topic") == topic
        for p in pending
    ), f"no chart_pending matching rendered topic {topic!r}; got {pending}"


@pytest.mark.asyncio
async def test_auto_chart_no_emit_hook_does_not_crash(tmp_path):
    """When the AppContext has no `_emit_event` (legacy callers / notebooks),
    the auto-chart path must still render without raising."""
    from types import SimpleNamespace
    from tools.agent_tools.auto_chart import _auto_chart_from_tool_outputs

    class _Logger:
        def __init__(self):
            self.events = []

        def log(self, evt, payload):
            self.events.append((evt, payload))

    case_folder = tmp_path / "CASE-NOEMIT"
    case_folder.mkdir()
    ctx = SimpleNamespace(
        logger=_Logger(),
        case_folder=case_folder,
        _specialist_kb={},
        _turn_id="t-noemit",
        _catalog=None,
        # no _emit_event attribute at all
    )
    tool_outputs = json.dumps({
        "series": [
            {"period": "2024-11", "raw_value": 720},
            {"period": "2024-12", "raw_value": 705},
            {"period": "2025-01", "raw_value": 690},
            {"period": "2025-02", "raw_value": 680},
        ],
        "value_column": "fico",
        "table": "model_scores",
    })
    n = await _auto_chart_from_tool_outputs(ctx, "modeling", tool_outputs)
    assert n >= 1
