"""Tests for tools/viz_renderer.py — Phase 2 chart rendering."""
import json
from pathlib import Path

import pytest

from tools.viz_renderer import (
    _resolve_axes,
    _slugify,
    kp_to_vega_spec,
    render_chart,
)


def test_slugify_strips_punctuation_and_caps_length():
    assert _slugify("monthly spend trend") == "monthly_spend_trend"
    assert _slugify("Top merchants — by sum!") == "Top_merchants_by_sum"
    assert _slugify("") == "kp"
    assert len(_slugify("x" * 200)) == 60


def test_resolve_axes_uses_explicit_when_present():
    """`y_field` (singular, back-compat) wraps to a single-element y_fields list."""
    numbers = [{"period": "2024-11", "value": 100, "extra": "ignored"}]
    assert _resolve_axes({"x_field": "period", "y_field": "value"}, numbers) == \
        ("period", ["value"])


def test_resolve_axes_handles_multi_series_y_fields():
    """`y_fields` (plural, new): every named field present in numbers becomes
    a series; missing fields are dropped."""
    numbers = [{"period": "2024-11", "spend": 300, "payment": 280}]
    assert _resolve_axes(
        {"x_field": "period", "y_fields": ["spend", "payment"]}, numbers
    ) == ("period", ["spend", "payment"])
    # Missing field gets dropped.
    assert _resolve_axes(
        {"x_field": "period", "y_fields": ["spend", "missing"]}, numbers
    ) == ("period", ["spend"])


def test_resolve_axes_falls_back_to_conventions():
    """When viz omits x_field/y_field(s), the renderer should pick `period`/`value`
    or `group`/`value` based on what's actually in the numbers dict."""
    trend_numbers = [{"period": "2024-11", "value": 100}]
    assert _resolve_axes({"kind": "trend"}, trend_numbers) == ("period", ["value"])
    breakdown_numbers = [{"group": "S BERTRAM", "value": 642000}]
    assert _resolve_axes({"kind": "share"}, breakdown_numbers) == ("group", ["value"])


def test_resolve_axes_returns_none_when_fields_missing():
    """Neither explicit fields nor fallbacks present → cannot render."""
    bad = [{"label": "X", "score": 5}]
    assert _resolve_axes({"kind": "trend"}, bad) is None


def test_render_chart_returns_none_on_unsupported_kind(tmp_path):
    kp = {"topic": "x", "viz": {"kind": "scatter"},
          "numbers": [{"period": "2024-11", "value": 100}]}
    assert render_chart(kp, tmp_path) is None


def test_render_chart_returns_none_when_no_numbers(tmp_path):
    kp = {"topic": "x", "viz": {"kind": "trend"}, "numbers": []}
    assert render_chart(kp, tmp_path) is None


def test_render_chart_returns_none_when_viz_missing(tmp_path):
    kp = {"topic": "x", "numbers": [{"period": "2024-11", "value": 100}]}
    assert render_chart(kp, tmp_path) is None


def test_render_chart_writes_png_for_trend(tmp_path):
    """Happy path — a trend KP produces a non-empty PNG at the expected path."""
    kp = {
        "topic": "monthly_spend_trend",
        "viz": {"kind": "trend", "x_field": "period", "y_field": "value"},
        "numbers": [
            {"period": "2024-11", "value": 300},
            {"period": "2024-12", "value": 500},
            {"period": "2025-01", "value": 800},
        ],
        "captured_at_turn": "abc123def",
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="abc123def")
    assert out is not None
    p = Path(out)
    assert p.exists()
    assert p.suffix == ".png"
    assert p.stat().st_size > 0
    # Filename pattern: <turn>-<topic>.png. Real turn ids are hex strings with
    # no hyphens (uuid.uuid4().hex[:12]); _slugify is identity on those.
    assert p.name.startswith("abc123def-")
    assert "monthly_spend_trend" in p.name


def test_render_chart_writes_png_for_bar(tmp_path):
    kp = {
        "topic": "top_merchants",
        "viz": {"kind": "bar", "x_field": "group", "y_field": "value"},
        "numbers": [
            {"group": "S BERTRAM", "value": 642000},
            {"group": "AMEXGIFTCARD.COM", "value": 215000},
        ],
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t1")
    assert out is not None
    assert Path(out).exists()


def test_render_chart_writes_png_for_share(tmp_path):
    kp = {
        "topic": "industry_mix",
        "viz": {"kind": "share"},
        "numbers": [
            {"group": "Industrial Supplies", "value": 450},
            {"group": "Gift Cards", "value": 200},
            {"group": "Restaurants", "value": 100},
        ],
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t1")
    assert out is not None
    assert Path(out).exists()


def test_render_chart_drops_non_numeric_y_values(tmp_path):
    """Mixed-validity series should still render using only the parseable
    rows. Empty y values get skipped silently."""
    kp = {
        "topic": "x",
        "viz": {"kind": "trend", "x_field": "period", "y_field": "value"},
        "numbers": [
            {"period": "2024-11", "value": 100},
            {"period": "2024-12", "value": "NaN"},  # skipped
            {"period": "2025-01", "value": 200},
        ],
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t1")
    assert out is not None  # rendered with 2 valid points


def test_render_chart_returns_none_when_all_y_unparseable(tmp_path):
    kp = {
        "topic": "x",
        "viz": {"kind": "trend", "x_field": "period", "y_field": "value"},
        "numbers": [
            {"period": "2024-11", "value": "bad"},
            {"period": "2024-12", "value": None},
        ],
    }
    assert render_chart(kp, tmp_path / "charts", turn_id="t1") is None


def test_render_chart_creates_charts_dir_lazily(tmp_path):
    """Charts directory is created on first render — no bootstrap needed."""
    charts_dir = tmp_path / "case_x" / "charts"
    assert not charts_dir.exists()
    kp = {"topic": "x", "viz": {"kind": "trend"},
          "numbers": [{"period": "p", "value": 1}, {"period": "q", "value": 2}]}
    out = render_chart(kp, charts_dir, turn_id="t1")
    assert out is not None
    assert charts_dir.exists()


def test_kp_to_vega_spec_emits_minimal_lite_v5_for_trend():
    kp = {
        "topic": "monthly_spend_trend",
        "viz": {"kind": "trend", "x_field": "period", "y_field": "value"},
        "numbers": [
            {"period": "2024-11", "value": 300},
            {"period": "2024-12", "value": 500},
        ],
    }
    spec = kp_to_vega_spec(kp)
    assert spec is not None
    assert spec["$schema"].endswith("v5.json")
    assert spec["mark"] == "line"
    assert spec["data"]["values"] == kp["numbers"]
    assert spec["encoding"]["x"]["field"] == "period"
    assert spec["encoding"]["y"]["field"] == "value"
    # Spec must roundtrip cleanly through JSON for storage in the KB / logs.
    assert json.loads(json.dumps(spec)) == spec


def test_kp_to_vega_spec_share_uses_horizontal_layout():
    """`share` kind maps to a bar mark with x/y swapped (horizontal bar)."""
    kp = {
        "topic": "industry_mix",
        "viz": {"kind": "share"},
        "numbers": [{"group": "A", "value": 100}, {"group": "B", "value": 50}],
    }
    spec = kp_to_vega_spec(kp)
    assert spec is not None
    assert spec["mark"] == "bar"
    # Horizontal: y is the categorical axis, x is quantitative.
    assert spec["encoding"]["y"]["field"] == "group"
    assert spec["encoding"]["x"]["field"] == "value"
    assert spec["encoding"]["y"]["sort"]["order"] == "descending"


def test_kp_to_vega_spec_returns_none_when_unviz_able():
    """Non-chartable KPs round-trip to None — symmetric with render_chart."""
    assert kp_to_vega_spec({"topic": "x"}) is None
    assert kp_to_vega_spec({"topic": "x", "viz": {"kind": "scatter"}}) is None
    assert kp_to_vega_spec({"topic": "x", "viz": {"kind": "trend"}, "numbers": []}) is None


def test_render_chart_writes_png_for_trend_dual(tmp_path):
    """trend_dual renders 2 series with mismatched scales on twin y-axes,
    same x-axis, single PNG output."""
    kp = {
        "topic": "score_vs_dpd",
        "viz": {
            "kind": "trend_dual",
            "x_field": "period",
            "y_fields": ["score", "dpd"],
        },
        "numbers": [
            {"period": "2024-11", "score": 720, "dpd": 0},
            {"period": "2024-12", "score": 705, "dpd": 15},
            {"period": "2025-01", "score": 690, "dpd": 30},
            {"period": "2025-02", "score": 680, "dpd": 45},
        ],
        "captured_at_turn": "t_dual",
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t_dual")
    assert out is not None
    p = Path(out)
    assert p.exists()
    assert p.suffix == ".png"
    assert p.stat().st_size > 0
    assert p.name.startswith("t_dual-")
    assert "score_vs_dpd" in p.name


def test_render_chart_trend_dual_returns_none_when_only_one_resolvable_series(tmp_path):
    """If one of the two y_fields is absent or all non-numeric, the dual
    layout collapses — bail rather than silently render a misleading
    single-line chart labelled `trend_dual`."""
    kp = {
        "topic": "broken_dual",
        "viz": {
            "kind": "trend_dual",
            "x_field": "period",
            "y_fields": ["score", "missing"],
        },
        "numbers": [
            {"period": "2024-11", "score": 720},
            {"period": "2024-12", "score": 705},
        ],
    }
    assert render_chart(kp, tmp_path / "charts", turn_id="t1") is None
