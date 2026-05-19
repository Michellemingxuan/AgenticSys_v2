"""Tests for tools/viz_renderer.py — Phase 2 chart rendering."""
import json
from pathlib import Path

import pytest

from tools.viz_renderer import (
    _resolve_axes,
    _slugify,
    _sort_points,
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


def test_render_chart_filename_collision_writes_dup_suffix(tmp_path):
    """When two KPs in the same turn share a topic slug (real bug from
    case-366132845011-815159: distiller put both CDSS and TSR under
    'model_scores_trend' and the second render overwrote the first),
    the renderer must write the second to a `__dup2.png` filename
    instead of silently overwriting. The KB dedup still strips the
    second downstream, but the PNG is preserved for forensics + the
    `viz_render_filename_collision` event lands in the JSONL."""
    logged: list = []

    class _MockLogger:
        def log(self, ev, payload):
            logged.append((ev, payload))

    kp = {
        "topic": "model_scores_trend",
        "viz": {"kind": "trend", "x_field": "period", "y_field": "value"},
        "numbers": [
            {"period": "2024-11", "value": 0.12},
            {"period": "2024-12", "value": 0.18},
            {"period": "2025-01", "value": 0.22},
            {"period": "2025-02", "value": 0.30},
        ],
        "captured_at_turn": "turnxyz",
    }

    first = render_chart(kp, tmp_path / "charts", turn_id="turnxyz",
                         logger=_MockLogger())
    second = render_chart(kp, tmp_path / "charts", turn_id="turnxyz",
                          logger=_MockLogger())

    assert first is not None and second is not None
    # Different files on disk — second did NOT overwrite first.
    assert Path(first).name != Path(second).name
    assert Path(first).exists() and Path(second).exists()
    assert Path(first).stat().st_size > 0 and Path(second).stat().st_size > 0
    # Second filename carries the dup-suffix marker so the collision is
    # visible from a directory listing alone.
    assert "__dup2.png" in Path(second).name
    # The collision is observable in the JSONL.
    assert any(ev == "viz_render_filename_collision" for ev, _ in logged)


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
    assert spec["data"]["values"] == kp["numbers"]
    # Trend now ships as a layered spec: [line, text] so each datapoint
    # gets a visible value label next to it. The line layer is first.
    assert isinstance(spec["layer"], list)
    assert len(spec["layer"]) == 2
    line_mark = spec["layer"][0]["mark"]
    text_mark = spec["layer"][1]["mark"]
    assert line_mark["type"] == "line" if isinstance(line_mark, dict) else line_mark == "line"
    assert text_mark["type"] == "text"
    assert spec["layer"][0]["encoding"]["x"]["field"] == "period"
    assert spec["layer"][0]["encoding"]["y"]["field"] == "value"
    # Text layer carries the value field — vega-embed renders these
    # next to each point.
    assert spec["layer"][1]["encoding"]["text"]["field"] == "value"
    # Spec must roundtrip cleanly through JSON for storage in the KB / logs.
    assert json.loads(json.dumps(spec)) == spec


def test_kp_to_vega_spec_share_uses_horizontal_layout():
    """`share` kind maps to a bar mark with x/y swapped (horizontal bar),
    layered with a text mark that prints the value at the end of each
    bar."""
    kp = {
        "topic": "industry_mix",
        "viz": {"kind": "share"},
        "numbers": [{"group": "A", "value": 100}, {"group": "B", "value": 50}],
    }
    spec = kp_to_vega_spec(kp)
    assert spec is not None
    assert isinstance(spec["layer"], list)
    assert len(spec["layer"]) == 2
    bar_layer = spec["layer"][0]
    text_layer = spec["layer"][1]
    assert bar_layer["mark"] == "bar"
    # Horizontal: y is the categorical axis, x is quantitative.
    assert bar_layer["encoding"]["y"]["field"] == "group"
    assert bar_layer["encoding"]["x"]["field"] == "value"
    assert bar_layer["encoding"]["y"]["sort"]["order"] == "descending"
    # Text layer offsets right of the bar with align=left.
    assert text_layer["mark"]["type"] == "text"
    assert text_layer["mark"]["align"] == "left"
    assert text_layer["encoding"]["text"]["field"] == "value"


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


def test_render_chart_trend_with_consistent_threshold_renders(tmp_path):
    """When every `numbers` row carries the same finite `threshold`,
    the renderer draws a horizontal dashed reference line on the chart.
    Verified by writing a PNG that's measurably larger than the same
    chart without the threshold (the extra line + label add pixels)."""
    base_numbers = [
        {"period": "2024-11", "value": 0.12},
        {"period": "2024-12", "value": 0.18},
        {"period": "2025-01", "value": 0.55},
        {"period": "2025-02", "value": 0.62},
    ]
    kp_no_t = {
        "topic": "score_no_threshold",
        "viz": {"kind": "trend", "x_field": "period", "y_fields": ["value"]},
        "numbers": base_numbers,
        "captured_at_turn": "t_no",
    }
    out_no = render_chart(kp_no_t, tmp_path / "no", turn_id="t_no")
    assert out_no is not None

    kp_with_t = {
        "topic": "score_with_threshold",
        "viz": {"kind": "trend", "x_field": "period", "y_fields": ["value"]},
        "numbers": [{**n, "threshold": 0.5} for n in base_numbers],
        "captured_at_turn": "t_yes",
    }
    out_with = render_chart(kp_with_t, tmp_path / "yes", turn_id="t_yes")
    assert out_with is not None

    # Threshold version is a separate PNG (different filename → no overwrite).
    assert Path(out_with).exists() and Path(out_no).exists()
    # And it has more pixels (rule + label add ink). Allow a generous
    # margin since matplotlib output isn't perfectly deterministic.
    size_no = Path(out_no).stat().st_size
    size_with = Path(out_with).stat().st_size
    assert size_with > size_no, (
        f"chart with threshold should be larger than without; "
        f"got {size_with} vs {size_no}"
    )


def test_render_chart_skips_threshold_when_inconsistent(tmp_path):
    """If rows disagree on threshold (or any row is missing it), do NOT
    draw a misleading single line. The chart still renders normally."""
    kp = {
        "topic": "varying_threshold",
        "viz": {"kind": "trend", "x_field": "period", "y_fields": ["value"]},
        "numbers": [
            {"period": "2024-11", "value": 0.12, "threshold": 0.5},
            {"period": "2024-12", "value": 0.18, "threshold": 0.6},  # disagree
            {"period": "2025-01", "value": 0.55},                     # missing
            {"period": "2025-02", "value": 0.62, "threshold": 0.5},
        ],
        "captured_at_turn": "t_var",
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t_var")
    assert out is not None  # chart still renders


def test_kp_to_vega_spec_trend_emits_rule_layer_for_threshold():
    """Vega-Lite spec must include a `rule` mark layer + a `text` label
    when the rows carry a consistent threshold."""
    kp = {
        "topic": "dpd_with_breach",
        "viz": {"kind": "trend", "x_field": "period", "y_fields": ["value"]},
        "numbers": [
            {"period": "2024-11", "value": 0, "threshold": 1},
            {"period": "2024-12", "value": 0, "threshold": 1},
            {"period": "2025-01", "value": 2, "threshold": 1},
            {"period": "2025-02", "value": 3, "threshold": 1},
        ],
    }
    spec = kp_to_vega_spec(kp)
    assert spec is not None
    layers = spec.get("layer", [])
    marks = [
        L["mark"]["type"] if isinstance(L["mark"], dict) else L["mark"]
        for L in layers
    ]
    assert "rule" in marks, f"expected a rule layer for the threshold; got {marks}"
    rule_layer = next(L for L in layers
                      if isinstance(L["mark"], dict) and L["mark"]["type"] == "rule")
    assert rule_layer["encoding"]["y"]["datum"] == 1


def test_render_chart_trend_dual_with_partial_missing_y_aligns_points(tmp_path):
    """Regression: case-aefd66 turn `5b8f94089581` failed with
    `ValueError: x and y must have same first dimension, but have shapes
    (4,) and (5,)`. The distiller produced a CDSS+TSR series where one
    period had a NaN/None TSR — `_extract_xy` independently dropped that
    entry from TSR's array but not CDSS's, yielding mismatched lengths
    that matplotlib rejected. After the fix, multi-series kinds pre-
    filter `numbers` to rows where ALL y_fields are valid, so both
    series share the same x-axis and the chart renders successfully.
    """
    kp = {
        "topic": "cdss_tsr_trajectory",
        "viz": {
            "kind": "trend_dual",
            "x_field": "period",
            "y_fields": ["credit_loss_prob", "tot_struct_risk_score"],
        },
        "numbers": [
            {"period": "2024-11", "credit_loss_prob": 0.12,
             "tot_struct_risk_score": 22.0},
            # ONE period with a missing TSR — pre-fix would mismatch.
            {"period": "2024-12", "credit_loss_prob": 0.18,
             "tot_struct_risk_score": None},
            {"period": "2025-01", "credit_loss_prob": 0.22,
             "tot_struct_risk_score": 28.0},
            {"period": "2025-02", "credit_loss_prob": 0.30,
             "tot_struct_risk_score": 32.0},
            {"period": "2025-03", "credit_loss_prob": 0.42,
             "tot_struct_risk_score": 38.0},
        ],
        "captured_at_turn": "t_align",
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t_align")
    assert out is not None, "trend_dual must render despite a single missing y on one series"
    p = Path(out)
    assert p.exists()
    assert p.stat().st_size > 0


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


def test_render_chart_writes_png_for_trend_grid(tmp_path):
    """trend_grid stacks one panel per y_field with a shared x-axis.
    Mismatched scales (score 0-1000 vs dpd 0-90 vs count 0-50) render
    correctly because each panel has its own y-scale."""
    kp = {
        "topic": "credit_risk_panel",
        "viz": {
            "kind": "trend_grid",
            "x_field": "period",
            "y_fields": ["tsr", "cdss", "transaction_count"],
        },
        "numbers": [
            {"period": "2024-11", "tsr": 720, "cdss": 680, "transaction_count": 42},
            {"period": "2024-12", "tsr": 705, "cdss": 665, "transaction_count": 38},
            {"period": "2025-01", "tsr": 690, "cdss": 650, "transaction_count": 35},
            {"period": "2025-02", "tsr": 680, "cdss": 640, "transaction_count": 31},
        ],
        "captured_at_turn": "t_grid",
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t_grid")
    assert out is not None
    p = Path(out)
    assert p.exists()
    assert p.suffix == ".png"
    assert p.stat().st_size > 0
    assert "credit_risk_panel" in p.name


def test_render_chart_trend_grid_drops_unparseable_series_silently(tmp_path):
    """If some series have only non-numeric values, those panels should
    drop out but the chart still renders for the panels that DID parse —
    same convention as multi-series `trend`."""
    kp = {
        "topic": "partial_grid",
        "viz": {
            "kind": "trend_grid",
            "x_field": "period",
            "y_fields": ["score", "broken"],
        },
        "numbers": [
            {"period": "2024-11", "score": 720, "broken": "n/a"},
            {"period": "2024-12", "score": 705, "broken": None},
        ],
    }
    out = render_chart(kp, tmp_path / "charts", turn_id="t1")
    assert out is not None  # score series rendered in its own panel


def test_kp_to_vega_spec_emits_layered_independent_y_for_trend_dual():
    """trend_dual → outer `layer` with TWO nested-layer groups, one per
    series. Each inner group wraps a line mark (+ optional threshold
    rule, when the rows carry `threshold_<y_field>`). Outer `resolve`
    keeps y-scales independent so the two axes appear on opposite sides;
    inner groups share their y-scale so a rule's `y.datum` lands on the
    correct line's axis.

    Without thresholds (this test): each inner group contains exactly
    ONE line mark, no text or rule. Tooltips on hover replace the
    overlap-prone static value labels."""
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
        ],
    }
    spec = kp_to_vega_spec(kp)
    assert spec is not None
    assert spec["$schema"].endswith("v5.json")
    assert spec["data"]["values"] == kp["numbers"]

    # Outer: 2 nested-layer groups (one per series).
    assert isinstance(spec.get("layer"), list)
    assert len(spec["layer"]) == 2
    line_marks: list[dict] = []
    text_marks: list[dict] = []
    rule_marks: list[dict] = []
    for outer in spec["layer"]:
        # Each outer entry must be a nested layer group.
        assert "layer" in outer, f"outer entry missing nested 'layer': {outer}"
        for inner in outer["layer"]:
            m = inner.get("mark")
            t = m["type"] if isinstance(m, dict) else m
            if t == "line":
                line_marks.append(inner)
            elif t == "text":
                text_marks.append(inner)
            elif t == "rule":
                rule_marks.append(inner)
    assert len(line_marks) == 2
    # No thresholds in this fixture, so no rule / text decoration.
    assert len(text_marks) == 0
    assert len(rule_marks) == 0

    # One y-field per line, on opposite sides of the chart.
    axes_orient = {
        L["encoding"]["y"]["field"]: L["encoding"]["y"]["axis"]["orient"]
        for L in line_marks
    }
    assert axes_orient == {"score": "left", "dpd": "right"}

    # Hover tooltip on each line carries x + y so the value reads on
    # mouseover (replaces the static text labels).
    for L in line_marks:
        tooltip = L["encoding"].get("tooltip")
        assert isinstance(tooltip, list) and len(tooltip) >= 2
        y_field = L["encoding"]["y"]["field"]
        assert any(t.get("field") == y_field for t in tooltip)

    # Legend on top, not in the right sidebar.
    for L in line_marks:
        legend = L["encoding"]["color"].get("legend")
        assert legend is not None
        assert legend.get("orient") == "top"

    # Outer composition keeps independent y-scales.
    assert spec["resolve"]["scale"]["y"] == "independent"
    # JSON-roundtrippable.
    assert json.loads(json.dumps(spec)) == spec


def test_kp_to_vega_spec_trend_dual_emits_per_axis_threshold_rules():
    """When rows carry per-axis `threshold_<y_field>` keys, each inner
    nested-layer group gains a `rule` mark at that threshold value, with
    color matching the series. The rule's y-scale is shared with the
    line in the same inner group (default `resolve.scale.y = "shared"`
    inside the group), so the rule lands at the correct y position even
    though the OUTER composition uses independent scales."""
    kp = {
        "topic": "cdss_tsr_with_thresholds",
        "viz": {
            "kind": "trend_dual",
            "x_field": "period",
            "y_fields": ["credit_loss_prob", "tot_struct_risk_score"],
        },
        "numbers": [
            {"period": "2024-11", "credit_loss_prob": 0.12,
             "tot_struct_risk_score": 22.0,
             "threshold_credit_loss_prob": 0.5,
             "threshold_tot_struct_risk_score": 20},
            {"period": "2024-12", "credit_loss_prob": 0.55,
             "tot_struct_risk_score": 35.0,
             "threshold_credit_loss_prob": 0.5,
             "threshold_tot_struct_risk_score": 20},
        ],
    }
    spec = kp_to_vega_spec(kp)
    assert spec is not None

    rules_by_y_field: dict[str, float] = {}
    for outer in spec["layer"]:
        line = next(
            inner for inner in outer["layer"]
            if isinstance(inner["mark"], dict) and inner["mark"]["type"] == "line"
        )
        y_field = line["encoding"]["y"]["field"]
        rule = next(
            (inner for inner in outer["layer"]
             if isinstance(inner["mark"], dict) and inner["mark"]["type"] == "rule"),
            None,
        )
        assert rule is not None, f"no rule in inner group for {y_field}"
        rules_by_y_field[y_field] = rule["encoding"]["y"]["datum"]

    assert rules_by_y_field == {
        "credit_loss_prob": 0.5,
        "tot_struct_risk_score": 20,
    }


def test_kp_to_vega_spec_emits_vconcat_for_trend_grid():
    """trend_grid → Vega-Lite `vconcat` of N panels, each layering a
    line and a text-label mark."""
    kp = {
        "topic": "credit_panel",
        "viz": {
            "kind": "trend_grid",
            "x_field": "period",
            "y_fields": ["tsr", "cdss", "txn_count"],
        },
        "numbers": [
            {"period": "2024-11", "tsr": 720, "cdss": 680, "txn_count": 42},
            {"period": "2024-12", "tsr": 705, "cdss": 665, "txn_count": 38},
        ],
    }
    spec = kp_to_vega_spec(kp)
    assert spec is not None
    assert isinstance(spec.get("vconcat"), list)
    assert len(spec["vconcat"]) == 3
    # Each sub-spec is a layered [line, text] pair against the shared x.
    fields = []
    for sub in spec["vconcat"]:
        assert isinstance(sub["layer"], list)
        assert len(sub["layer"]) == 2
        line_layer, text_layer = sub["layer"]
        assert line_layer["mark"]["type"] == "line"
        assert text_layer["mark"]["type"] == "text"
        assert line_layer["encoding"]["x"]["field"] == "period"
        fields.append(line_layer["encoding"]["y"]["field"])
        # Text layer carries the same y-field as text.
        assert text_layer["encoding"]["text"]["field"] == line_layer["encoding"]["y"]["field"]
    assert fields == ["tsr", "cdss", "txn_count"]
    assert json.loads(json.dumps(spec)) == spec


# ── _sort_points: temporal / numeric / ranking / alpha ───────────────────────


def test_sort_points_temporal_when_x_parses_as_date():
    """Mixed-order dates → chronological ascending. This is what
    prevents the back-and-forth zig-zag a trend line picks up when the
    specialist hands the points in summary-call order instead of
    chronological order."""
    points = [
        {"period": "2025-02", "value": 200},
        {"period": "2024-11", "value": 50},
        {"period": "2025-01", "value": 175},
        {"period": "2024-12", "value": 120},
    ]
    out = _sort_points(points, "period", ["value"], "trend")
    assert [p["period"] for p in out] == ["2024-11", "2024-12", "2025-01", "2025-02"]


def test_sort_points_numeric_when_x_parses_as_number():
    """Numeric x → ascending. Catches things like `score_band: 1..5`."""
    points = [
        {"band": 5, "share": 0.1},
        {"band": 2, "share": 0.4},
        {"band": 1, "share": 0.5},
        {"band": 3, "share": 0.3},
    ]
    out = _sort_points(points, "band", ["share"], "trend")
    assert [p["band"] for p in out] == [1, 2, 3, 5]


def test_sort_points_ranking_for_categorical_share():
    """`share` with categorical x → biggest first (descending by y).
    Matches the existing horizontal-bar sort the share renderer does
    internally — the sort layer just makes it the ALSO the order for
    every consumer (Vega spec, downstream table rendering, etc.)."""
    points = [
        {"merchant": "C", "value": 100},
        {"merchant": "A", "value": 500},
        {"merchant": "B", "value": 250},
        {"merchant": "D", "value": 50},
    ]
    out = _sort_points(points, "merchant", ["value"], "share")
    assert [p["merchant"] for p in out] == ["A", "B", "C", "D"]


def test_sort_points_alpha_fallback():
    """Categorical x with a `trend` kind (where no ranking semantics
    apply) → alpha ascending. Stable tie-breaking on equal y."""
    points = [
        {"label": "delta", "value": 1},
        {"label": "alpha", "value": 2},
        {"label": "charlie", "value": 3},
        {"label": "bravo", "value": 4},
    ]
    out = _sort_points(points, "label", ["value"], "trend")
    assert [p["label"] for p in out] == ["alpha", "bravo", "charlie", "delta"]


# ── kind="table" sanity ──────────────────────────────────────────────────────


def test_render_chart_skips_table_kind():
    """`kind='table'` is handled at the make_chart-tool layer (the row
    data goes straight to the SSE payload, no matplotlib render).
    `render_chart` itself should be a no-op — return None without
    raising so a stray call from a legacy code path doesn't crash."""
    kp = {
        "topic": "tiny_breakdown",
        "viz": {"kind": "table", "x_field": "month", "y_fields": ["spend"]},
        "numbers": [
            {"month": "2025-05", "spend": 404152},
            {"month": "2025-06", "spend": 219000},
        ],
    }
    # Renderer treats `table` as unsupported (it isn't in _SUPPORTED_KINDS)
    # and returns None — no exception, no PNG.
    assert render_chart(kp, Path("/tmp"), turn_id="t1") is None


def test_kp_to_vega_spec_skips_table_kind():
    kp = {
        "topic": "tiny_breakdown",
        "viz": {"kind": "table", "x_field": "month", "y_fields": ["spend"]},
        "numbers": [{"month": "2025-05", "spend": 404152}],
    }
    assert kp_to_vega_spec(kp) is None
