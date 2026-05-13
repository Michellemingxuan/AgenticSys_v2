"""Tests for tools/data_viz_tools.py — specialist-callable charting tool."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents import RunContextWrapper

from tools.data_viz_tools import build_make_chart_tool


class _Logger:
    def __init__(self):
        self.events = []

    def log(self, evt, payload):
        self.events.append((evt, payload))


def _make_ctx(tmp_path: Path) -> SimpleNamespace:
    """AppContext-shaped stand-in carrying everything make_chart reads."""
    case_folder = tmp_path / "CASE-VIZ"
    case_folder.mkdir(exist_ok=True)
    return SimpleNamespace(
        logger=_Logger(),
        case_folder=case_folder,
        _specialist_kb={},
        _turn_id="abc123",
    )


def _good_points():
    return [
        {"period": "2024-11", "value": 300},
        {"period": "2024-12", "value": 500},
        {"period": "2025-01", "value": 800},
    ]


@pytest.mark.asyncio
async def test_make_chart_writes_png_and_persists_kp(tmp_path):
    """Happy path: tool renders the PNG, builds vega_spec, and writes a
    KP into ``_specialist_kb[<specialist>]`` with image_path populated."""
    tool = build_make_chart_tool("spend_payments")
    ctx = _make_ctx(tmp_path)

    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "monthly_spend_trend",
            "kind": "trend",
            "claim": "Spend rose 2.7× from Nov-2024 to Jan-2025.",
            "points": _good_points(),
            "x_field": "period",
            "y_fields": ["value"],
            "source_call": "summarize_trend('spends','Amount','Date',period='month',op='sum')",
        }),
    )

    assert "[chart created]" in out
    assert "monthly_spend_trend" in out

    # KP persisted under the specialist's key.
    kb = ctx._specialist_kb
    assert "spend_payments" in kb
    assert len(kb["spend_payments"]) == 1
    kp = kb["spend_payments"][0]

    # Required KP fields all present.
    assert kp["topic"] == "monthly_spend_trend"
    assert kp["claim"].startswith("Spend rose")
    assert kp["captured_at_turn"] == "abc123"
    assert kp["confidence"] == "high"
    assert kp["viz"] == {"kind": "trend", "x_field": "period", "y_fields": ["value"]}
    assert kp["numbers"] == _good_points()

    # Chart artifacts produced.
    assert "image_path" in kp
    img = Path(kp["image_path"])
    assert img.exists() and img.suffix == ".png" and img.stat().st_size > 0
    assert kp["vega_spec"]["mark"] == "line"

    # Tool-invocation event logged.
    assert any(e[0] == "make_chart_tool_invoked" for e in ctx.logger.events)


@pytest.mark.asyncio
async def test_make_chart_rejects_bad_kind(tmp_path):
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "scatter",
            "claim": "anything", "points": _good_points(),
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out
    assert "kind" in out
    # Nothing written.
    assert ctx._specialist_kb == {}


@pytest.mark.asyncio
async def test_make_chart_rejects_too_few_points(tmp_path):
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend",
            "claim": "c", "points": [{"period": "2024-11", "value": 1}],
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out
    assert "2+ dicts" in out


@pytest.mark.asyncio
async def test_make_chart_rejects_blank_topic_or_claim(tmp_path):
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "  ", "kind": "trend", "claim": "fine",
            "points": _good_points(),
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out
    assert "topic" in out


@pytest.mark.asyncio
async def test_make_chart_returns_error_on_bad_axes(tmp_path):
    """Renderer cannot find the named fields → tool returns a guidance
    string the LLM can act on, no KP written."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "bad_axes", "kind": "trend", "claim": "c",
            "points": [{"label": "a", "score": 1}, {"label": "b", "score": 2}],
            "x_field": "period", "y_fields": ["value"],   # neither key in points
            "source_call": "",
        }),
    )
    assert "[make_chart error]" in out
    assert "renderer" in out.lower()
    assert ctx._specialist_kb == {}


@pytest.mark.asyncio
async def test_make_chart_no_session_returns_clear_error():
    """Tests / legacy paths without a session must NOT pretend to chart."""
    tool = build_make_chart_tool("modeling")
    ctx = SimpleNamespace(
        logger=_Logger(), case_folder=None,
        _specialist_kb=None, _turn_id=None,
    )
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend", "claim": "c",
            "points": _good_points(),
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out
    assert "session" in out.lower()


@pytest.mark.asyncio
async def test_make_chart_multi_series_renders_one_chart_with_all_fields(tmp_path):
    """Two y_fields produce a SINGLE multi-line trend chart, not two charts.
    The KP's viz spec carries the list verbatim so downstream renderers
    (Vega-Lite spec) can reproduce the same multi-series shape."""
    tool = build_make_chart_tool("spend_payments")
    ctx = _make_ctx(tmp_path)
    points = [
        {"period": "2024-11", "spend": 300, "payment": 280},
        {"period": "2024-12", "spend": 500, "payment": 420},
        {"period": "2025-01", "spend": 800, "payment": 510},
    ]
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "spend_vs_payment",
            "kind": "trend",
            "claim": "Spend rose faster than payment over Nov-Jan.",
            "points": points,
            "x_field": "period",
            "y_fields": ["spend", "payment"],
            "source_call": "",
        }),
    )
    assert "[chart created]" in out
    # Surfaces the multi-series count.
    assert "× 2 series" in out

    kp = ctx._specialist_kb["spend_payments"][0]
    assert kp["viz"] == {
        "kind": "trend", "x_field": "period",
        "y_fields": ["spend", "payment"],
    }
    # Vega-Lite spec uses fold transform for multi-series.
    assert kp["vega_spec"]["transform"][0]["fold"] == ["spend", "payment"]
    # Single PNG file — not two.
    img = Path(kp["image_path"])
    assert img.exists() and img.stat().st_size > 0


@pytest.mark.asyncio
async def test_make_chart_share_kind_rejects_multi_series(tmp_path):
    """share (horizontal-bar breakdown) is single-series only — multi-series
    via grouped bars should use kind='bar' instead."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "share", "claim": "c",
            "points": [{"group": "A", "x": 1, "y": 2},
                       {"group": "B", "x": 3, "y": 4}],
            "x_field": "group", "y_fields": ["x", "y"],
            "source_call": "",
        }),
    )
    assert "[make_chart error]" in out
    assert "single-series" in out
    assert ctx._specialist_kb == {}


@pytest.mark.asyncio
async def test_make_chart_factory_isolates_specialist_kbs(tmp_path):
    """Two specialists' tools must write into their own KB list, never
    cross-contaminate each other's namespace."""
    tool_a = build_make_chart_tool("alpha")
    tool_b = build_make_chart_tool("beta")
    ctx = _make_ctx(tmp_path)

    payload = {
        "topic": "shared_topic", "kind": "trend", "claim": "c",
        "points": _good_points(),
        "x_field": "period", "y_fields": ["value"], "source_call": "",
    }
    await tool_a.on_invoke_tool(RunContextWrapper(ctx), json.dumps(payload))
    await tool_b.on_invoke_tool(RunContextWrapper(ctx), json.dumps(payload))

    assert set(ctx._specialist_kb.keys()) == {"alpha", "beta"}
    assert len(ctx._specialist_kb["alpha"]) == 1
    assert len(ctx._specialist_kb["beta"]) == 1


@pytest.mark.asyncio
async def test_trend_dual_requires_exactly_two_y_fields(tmp_path):
    """trend_dual is twin-y — exactly 2 series. 1 series → tell the
    model to use plain `trend`; 3+ → tell it to use `trend_grid`."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)

    # 1 y_field is too few.
    out_one = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend_dual", "claim": "c",
            "points": _good_points(),
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out_one
    assert "exactly 2" in out_one
    assert "kind='trend'" in out_one  # specifically steers to plain trend

    # 3 y_fields is too many.
    points_three = [
        {"period": "2024-11", "a": 1, "b": 2, "c": 3},
        {"period": "2024-12", "a": 2, "b": 3, "c": 4},
    ]
    out_three = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend_dual", "claim": "c",
            "points": points_three,
            "x_field": "period", "y_fields": ["a", "b", "c"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out_three
    assert "trend_grid" in out_three  # points the model at the right alternative
    assert ctx._specialist_kb == {}


@pytest.mark.asyncio
async def test_trend_grid_requires_two_to_six_y_fields(tmp_path):
    """trend_grid stacks N panels — 1 series is just a `trend`, 7+ stops
    being readable."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)

    # 1 y_field — error pointing back to trend.
    out_one = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend_grid", "claim": "c",
            "points": _good_points(),
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out_one
    assert "between 2 and 6" in out_one  # mentions the actual bounds

    # 7 y_fields — error citing the upper bound.
    keys = ["a", "b", "c", "d", "e", "f", "g"]
    points_seven = [
        {"period": "2024-11", **{k: i for i, k in enumerate(keys)}},
        {"period": "2024-12", **{k: i + 1 for i, k in enumerate(keys)}},
    ]
    out_seven = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend_grid", "claim": "c",
            "points": points_seven,
            "x_field": "period", "y_fields": keys, "source_call": "",
        }),
    )
    assert "[make_chart error]" in out_seven
    assert "between 2 and 6" in out_seven  # mentions the actual bounds
    assert ctx._specialist_kb == {}
