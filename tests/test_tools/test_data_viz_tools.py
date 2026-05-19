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


def _make_ctx(tmp_path: Path, capture_emits: list | None = None) -> SimpleNamespace:
    """AppContext-shaped stand-in carrying everything make_chart reads.

    Pass ``capture_emits`` (a list) to record any SSE events the tool
    publishes via `_emit_event`. Each appended entry is `(event, payload)`.
    """
    case_folder = tmp_path / "CASE-VIZ"
    case_folder.mkdir(exist_ok=True)
    emit_fn = (
        (lambda evt, payload: capture_emits.append((evt, payload)))
        if capture_emits is not None else None
    )
    return SimpleNamespace(
        logger=_Logger(),
        case_folder=case_folder,
        _specialist_kb={},
        _turn_id="abc123",
        _emit_event=emit_fn,
    )


def _good_points():
    # 4+ entries to satisfy the validator's `_PLOT_MIN_POINTS` floor
    # (plot kinds require ≥ 4; 1-3 row datasets must use kind='table').
    return [
        {"period": "2024-11", "value": 300},
        {"period": "2024-12", "value": 500},
        {"period": "2025-01", "value": 800},
        {"period": "2025-02", "value": 900},
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
    # Vega spec is layered [line, text] so each datapoint carries an
    # exact-value label next to it.
    assert isinstance(kp["vega_spec"]["layer"], list)
    line_layer = kp["vega_spec"]["layer"][0]
    line_mark = line_layer["mark"]
    assert (line_mark["type"] if isinstance(line_mark, dict) else line_mark) == "line"

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
    """Plot kinds need ≥ 4 datapoints — the validator points the model at
    kind='table' for 1-3 rows so the rows surface as a table card
    instead of an unreadable plot."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    # 2 points — would pass the "1+ dicts" basic check but fail the
    # per-plot-kind minimum.
    out_two = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend",
            "claim": "c",
            "points": [
                {"period": "2024-11", "value": 1},
                {"period": "2024-12", "value": 2},
            ],
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out_two
    assert "at least 4 datapoints" in out_two
    assert "kind='table'" in out_two

    # Empty list — fails the basic 1+ dicts check.
    out_zero = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend",
            "claim": "c", "points": [],
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out_zero
    assert "1+ dicts" in out_zero


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
    # 4 points so we pass the points-count validator and reach the renderer.
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "bad_axes", "kind": "trend", "claim": "c",
            "points": [
                {"label": "a", "score": 1},
                {"label": "b", "score": 2},
                {"label": "c", "score": 3},
                {"label": "d", "score": 4},
            ],
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
        {"period": "2025-02", "spend": 900, "payment": 600},
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
            "points": [
                {"group": "A", "x": 1, "y": 2},
                {"group": "B", "x": 3, "y": 4},
                {"group": "C", "x": 5, "y": 6},
                {"group": "D", "x": 7, "y": 8},
            ],
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

    # 3 y_fields is too many. 4+ rows so we pass the points-count
    # validator and reach the per-kind y_fields check.
    points_three = [
        {"period": "2024-11", "a": 1, "b": 2, "c": 3},
        {"period": "2024-12", "a": 2, "b": 3, "c": 4},
        {"period": "2025-01", "a": 3, "b": 4, "c": 5},
        {"period": "2025-02", "a": 4, "b": 5, "c": 6},
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

    # 7 y_fields — error citing the upper bound. 4+ rows so we pass
    # the points-count validator and reach the per-kind y_fields check.
    keys = ["a", "b", "c", "d", "e", "f", "g"]
    points_seven = [
        {"period": "2024-11", **{k: i for i, k in enumerate(keys)}},
        {"period": "2024-12", **{k: i + 1 for i, k in enumerate(keys)}},
        {"period": "2025-01", **{k: i + 2 for i, k in enumerate(keys)}},
        {"period": "2025-02", **{k: i + 3 for i, k in enumerate(keys)}},
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


@pytest.mark.asyncio
async def test_make_chart_trend_dual_end_to_end(tmp_path):
    """Specialist call: kind=trend_dual with 2 y_fields → single PNG,
    KP persisted with viz spec carrying y_fields verbatim and vega_spec
    using `resolve.scale.y == 'independent'`."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    points = [
        {"period": "2024-11", "score": 720, "dpd": 0},
        {"period": "2024-12", "score": 705, "dpd": 15},
        {"period": "2025-01", "score": 690, "dpd": 30},
        {"period": "2025-02", "score": 680, "dpd": 45},
    ]
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "score_vs_dpd",
            "kind": "trend_dual",
            "claim": "Score declined as DPD climbed over Nov-Feb.",
            "points": points,
            "x_field": "period",
            "y_fields": ["score", "dpd"],
            "source_call": "summarize_trend(...) x 2 merged",
        }),
    )
    assert "[chart created]" in out
    assert "× 2 series" in out

    kp = ctx._specialist_kb["modeling"][0]
    assert kp["viz"] == {
        "kind": "trend_dual", "x_field": "period",
        "y_fields": ["score", "dpd"],
    }
    img = Path(kp["image_path"])
    assert img.exists() and img.stat().st_size > 0
    # Vega spec uses independent y resolve.
    assert kp["vega_spec"]["resolve"]["scale"]["y"] == "independent"


@pytest.mark.asyncio
async def test_make_chart_trend_grid_end_to_end(tmp_path):
    """Specialist call: kind=trend_grid with 3 y_fields → single PNG (one
    file, not three), KP persisted with vconcat-shaped vega_spec."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    points = [
        {"period": "2024-11", "tsr": 720, "cdss": 680, "txn_count": 42},
        {"period": "2024-12", "tsr": 705, "cdss": 665, "txn_count": 38},
        {"period": "2025-01", "tsr": 690, "cdss": 650, "txn_count": 35},
        {"period": "2025-02", "tsr": 680, "cdss": 640, "txn_count": 31},
    ]
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "credit_risk_panel",
            "kind": "trend_grid",
            "claim": "All three risk indicators deteriorated together.",
            "points": points,
            "x_field": "period",
            "y_fields": ["tsr", "cdss", "txn_count"],
            "source_call": "summarize_trend(...) x 3 merged",
        }),
    )
    assert "[chart created]" in out
    assert "× 3 series" in out

    kp = ctx._specialist_kb["modeling"][0]
    assert kp["viz"]["kind"] == "trend_grid"
    assert kp["viz"]["y_fields"] == ["tsr", "cdss", "txn_count"]
    img = Path(kp["image_path"])
    assert img.exists() and img.stat().st_size > 0
    # Vega spec is vconcat of 3 line panels.
    assert len(kp["vega_spec"]["vconcat"]) == 3


@pytest.mark.asyncio
async def test_make_chart_emits_chart_pending_before_render(tmp_path):
    """When the AppContext exposes an `_emit_event` hook, `make_chart` fires
    a `chart_pending` event the moment validation passes — BEFORE the
    PNG render runs. The frontend uses this to show a "working on the
    plots" placeholder while the specialist's render completes (and the
    actual `chart` SSE event lands later, at end-of-turn).

    Payload contract: (specialist, topic, kind) so the frontend can match
    the placeholder to the eventual chart event by (specialist, topic).
    """
    emits: list = []
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path, capture_emits=emits)

    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "score_vs_dpd",
            "kind": "trend_dual",
            "claim": "Score declined as DPD climbed.",
            "points": [
                {"period": "2024-11", "score": 720, "dpd": 0},
                {"period": "2024-12", "score": 705, "dpd": 15},
                {"period": "2025-01", "score": 690, "dpd": 30},
                {"period": "2025-02", "score": 680, "dpd": 45},
            ],
            "x_field": "period",
            "y_fields": ["score", "dpd"],
            "source_call": "summarize_trend(...)",
        }),
    )
    assert "[chart created]" in out

    # Exactly one pending event, fired with the right shape.
    pending = [e for e in emits if e[0] == "chart_pending"]
    assert len(pending) == 1
    payload = pending[0][1]
    assert payload == {
        "specialist": "modeling",
        "topic": "score_vs_dpd",
        "kind": "trend_dual",
    }


@pytest.mark.asyncio
async def test_make_chart_skips_chart_pending_when_no_emit_hook(tmp_path):
    """When the AppContext has no `_emit_event` (legacy callers, notebooks,
    or pre-wiring code paths), `make_chart` must NOT crash — it just
    skips the pending emit and proceeds to render."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)  # capture_emits=None → no emit hook
    assert ctx._emit_event is None

    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "trend", "claim": "c",
            "points": _good_points(),
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[chart created]" in out


@pytest.mark.asyncio
async def test_make_chart_skips_pending_event_when_validation_fails(tmp_path):
    """Failed validation must NOT emit `chart_pending` — the placeholder
    on the frontend would never be replaced because the actual `chart`
    event will never fire for an invalid request."""
    emits: list = []
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path, capture_emits=emits)

    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "x", "kind": "scatter",  # invalid kind
            "claim": "c", "points": _good_points(),
            "x_field": "period", "y_fields": ["value"], "source_call": "",
        }),
    )
    assert "[make_chart error]" in out
    # No pending event was published.
    assert [e for e in emits if e[0] == "chart_pending"] == []


@pytest.mark.asyncio
async def test_make_chart_table_kind_skips_render_and_persists_kp(tmp_path):
    """`kind='table'` short-circuits the matplotlib render — no PNG file
    is produced, no Vega spec attached, but the KP IS persisted so the
    server's chart-collection path emits it as a chart event with the
    row data inline. Used for 1-3 row datasets where a plot would just
    be noise (e.g. last-3-months summary, two-period comparison)."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    rows = [
        {"month": "2025-05", "spend": 404152},
        {"month": "2025-06", "spend": 219000},
    ]
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "spend_last_two_months",
            "kind": "table",
            "claim": "Spend halved May → June 2025.",
            "points": rows,
            "x_field": "month",
            "y_fields": ["spend"],
            "source_call": "summarize_trend('spends', 'Amount', 'Date', "
                           "period='month', op='sum')",
        }),
    )
    assert "[chart created]" in out
    assert "table" in out  # tool surfaces the kind in its return message

    kp = ctx._specialist_kb["modeling"][0]
    assert kp["viz"]["kind"] == "table"
    assert kp["numbers"] == rows
    # No image rendered.
    assert kp.get("image_path") is None
    # No Vega spec attached (table is HTML-rendered on the frontend, not
    # via vega-embed).
    assert kp.get("vega_spec") is None


@pytest.mark.asyncio
async def test_make_chart_table_kind_accepts_one_row(tmp_path):
    """Table kind has no minimum-row count — a single-row table is
    still useful (e.g. cell-level fact box). Bypasses the
    `_PLOT_MIN_POINTS=4` rule that the other kinds are subject to."""
    tool = build_make_chart_tool("modeling")
    ctx = _make_ctx(tmp_path)
    out = await tool.on_invoke_tool(
        RunContextWrapper(ctx),
        json.dumps({
            "topic": "single_fact", "kind": "table", "claim": "Latest score.",
            "points": [{"month": "2025-07", "score": 642}],
            "x_field": "month", "y_fields": ["score"],
            "source_call": "",
        }),
    )
    assert "[chart created]" in out
    assert ctx._specialist_kb["modeling"][0]["numbers"] == [
        {"month": "2025-07", "score": 642},
    ]
