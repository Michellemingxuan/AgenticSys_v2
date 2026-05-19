# Multi-Variable Chart Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Project rule:** Per `.claude/memory/feedback_commit_only_when_asked.md`, do NOT run the commit step at the end of each task automatically. Stage and stop; the user runs `git commit` when they're ready. Each task's commit step shows the message to use when the user approves.

**Goal:** Add two new `make_chart` kinds — `trend_dual` (twin-y, exactly 2 series) and `trend_grid` (faceted, N stacked panels with shared x) — so specialists can plot multiple variables on the same time axis without losing signal to scale mismatch.

**Architecture:** Extend the existing tool/renderer pipeline. The `make_chart` function tool adds the new kinds to its validator. `render_chart` gains two new render branches that reuse `_resolve_axes` / `_extract_xy` / `_PALETTE` / `_apply_style`. `kp_to_vega_spec` gains parallel paths emitting `resolve.scale.y: independent` (dual) and `vconcat` (grid). The skill `data_query.md` is updated with a decision rule that names the three modes. No changes to the KB / `_collect_turn_charts` / serving path.

**Tech Stack:** Python 3.11+, matplotlib (Agg backend), Vega-Lite v5, openai-agents SDK function-tool, pytest + pytest-asyncio.

**Spec:** `docs/specs/2026-05-13-multi-variable-chart-modes-design.md`

---

## File Structure

**Modify:**
- `tools/data_viz_tools.py` — extend `_VALID_KINDS`, add per-kind y_fields validation, update tool description override.
- `tools/viz_renderer.py` — add `trend_dual` and `trend_grid` branches to `render_chart`; add matching paths in `kp_to_vega_spec`; extend `_SUPPORTED_KINDS`.
- `skills/workflow/data_query.md` — replace the multi-series paragraph in § Output formatting / Charting with the three-mode decision rule.

**Modify (tests):**
- `tests/test_tools/test_data_viz_tools.py` — validator cases for new kinds; happy-path end-to-end cases for `trend_dual` / `trend_grid`.
- `tests/test_tools/test_viz_renderer.py` — render-path and Vega-spec cases for the new kinds.

**Create:** none. All work lives in existing files.

---

## Task 1: Validator accepts new kinds and enforces y_fields counts

Add `trend_dual` and `trend_grid` to `_VALID_KINDS`. Add per-kind y_fields-count validation: `trend_dual` requires exactly 2; `trend_grid` requires 2–6. Renderer changes come in later tasks — this task tests only the validator surface.

**Files:**
- Modify: `tools/data_viz_tools.py`
- Test: `tests/test_tools/test_data_viz_tools.py`

- [ ] **Step 1.1: Write failing validator tests**

Append to `tests/test_tools/test_data_viz_tools.py`:

```python
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
    assert "trend" in out_one  # points the model back to plain trend

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
    assert "2" in out_one  # mentions the lower bound

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
    assert "6" in out_seven  # mentions the upper bound
    assert ctx._specialist_kb == {}
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/test_tools/test_data_viz_tools.py::test_trend_dual_requires_exactly_two_y_fields tests/test_tools/test_data_viz_tools.py::test_trend_grid_requires_two_to_six_y_fields -v`

Expected: FAIL — both tests reject `kind` because `trend_dual` / `trend_grid` aren't in `_VALID_KINDS` yet, so the assertions on the *specific* error strings ("exactly 2", "trend_grid", "6") won't match the generic kind-rejection message.

- [ ] **Step 1.3: Extend `_VALID_KINDS` and add per-kind y_fields validation**

In `tools/data_viz_tools.py`, replace the `_VALID_KINDS` constant near the top:

```python
_VALID_KINDS = ("trend", "bar", "share", "trend_dual", "trend_grid")
```

Then, inside `make_chart`, AFTER the existing block that validates `y_fields` is a non-empty list (currently ending with the `share` + len(y_fields) > 1 guard around lines 88–100), add:

```python
        if kind == "trend_dual" and len(y_fields) != 2:
            return (
                f"[make_chart error] `trend_dual` (twin y-axis) requires "
                f"exactly 2 entries in `y_fields`; got {len(y_fields)}. "
                f"Use `kind='trend'` for a single shared y-axis with 1 or "
                f"more series on the same scale, or `kind='trend_grid'` "
                f"for 3+ series on different scales."
            )
        if kind == "trend_grid" and not (2 <= len(y_fields) <= 6):
            return (
                f"[make_chart error] `trend_grid` (stacked faceted panels) "
                f"requires between 2 and 6 entries in `y_fields`; got "
                f"{len(y_fields)}. Use `kind='trend'` for a single series, "
                f"or drop the lowest-signal series if you have 7+."
            )
```

- [ ] **Step 1.4: Run new tests to verify they pass**

Run: `pytest tests/test_tools/test_data_viz_tools.py::test_trend_dual_requires_exactly_two_y_fields tests/test_tools/test_data_viz_tools.py::test_trend_grid_requires_two_to_six_y_fields -v`

Expected: PASS — both tests.

- [ ] **Step 1.5: Run the full data_viz_tools test file to confirm no regression**

Run: `pytest tests/test_tools/test_data_viz_tools.py -v`

Expected: all existing tests still pass.

- [ ] **Step 1.6: Stage and prepare commit (DO NOT run `git commit` without user approval)**

```bash
git add tools/data_viz_tools.py tests/test_tools/test_data_viz_tools.py
```

Commit message to use when user approves:
```
feat(viz): accept trend_dual / trend_grid kinds with y_fields-count validation
```

---

## Task 2: Renderer — `trend_dual` branch (twin y-axis, 2 series)

Add the matplotlib render path for `trend_dual` in `render_chart`. One subplot, primary y on left axis, secondary y on `ax.twinx()`, shared x-ticks. Single combined legend. Y-axis label colors match line colors.

**Files:**
- Modify: `tools/viz_renderer.py`
- Test: `tests/test_tools/test_viz_renderer.py`

- [ ] **Step 2.1: Write failing renderer test**

Append to `tests/test_tools/test_viz_renderer.py`:

```python
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
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `pytest tests/test_tools/test_viz_renderer.py::test_render_chart_writes_png_for_trend_dual tests/test_tools/test_viz_renderer.py::test_render_chart_trend_dual_returns_none_when_only_one_resolvable_series -v`

Expected: FAIL — `trend_dual` isn't in `_SUPPORTED_KINDS`, so `render_chart` returns None before reaching any new branch, and the happy-path test fails on `assert out is not None`.

- [ ] **Step 2.3: Add `trend_dual` to `_SUPPORTED_KINDS` and implement the render branch**

In `tools/viz_renderer.py`, update the constant near the top:

```python
_SUPPORTED_KINDS = {"trend", "bar", "share", "trend_dual", "trend_grid"}
```

Inside `render_chart`, find the `else:  # "share" — horizontal bar, single series only` block and add a new branch BEFORE it (so the order is: `if kind == "trend"` → `elif kind == "bar"` → `elif kind == "trend_dual"` → `elif kind == "trend_grid"` → `else: # share`). Add the `trend_dual` branch:

```python
        elif kind == "trend_dual":
            # Two series on twin y-axes. trend_dual ENFORCES that both
            # extracted series exist and align on the same x — if one is
            # missing or all-unparseable, _extract_xy returned None and the
            # extracted list is shorter than 2; bail to None so we don't
            # silently mislabel a 1-line chart as `trend_dual`.
            if len(extracted) != 2:
                if logger is not None:
                    logger.log("viz_render_skipped",
                               {"reason": "trend_dual_needs_two_series",
                                "topic": kp.get("topic"),
                                "n_resolved": len(extracted)})
                try:
                    plt.close(fig)
                except Exception:
                    pass
                return None

            (yf1, (xs_first, ys1)) = extracted[0]
            (yf2, (_, ys2)) = extracted[1]
            indices = list(range(len(xs_first)))

            primary_color = _PALETTE[0]
            secondary_color = _PALETTE[1]

            line1, = ax.plot(indices, ys1, marker="o", linewidth=2.0,
                             markersize=5.5, color=primary_color, label=yf1)
            ax2 = ax.twinx()
            line2, = ax2.plot(indices, ys2, marker="s", linewidth=2.0,
                              markersize=5.5, color=secondary_color, label=yf2)

            ax.set_xticks(indices)
            n = len(xs_first)
            stride = max(1, n // 10)
            visible = [str(xs_first[i]) if (i % stride == 0 or i == n - 1) else ""
                       for i in indices]
            ax.set_xticklabels(visible, rotation=30, ha="right", fontsize=9)
            ax.set_xlabel(x_field)

            # Label each y-axis with its field name, color-matched to the
            # corresponding line so the reader maps line→axis at a glance.
            ax.set_ylabel(yf1, color=primary_color)
            ax2.set_ylabel(yf2, color=secondary_color)
            ax.tick_params(axis="y", colors=primary_color)
            ax2.tick_params(axis="y", colors=secondary_color)

            # Compact value formatting on both axes.
            ax.yaxis.set_major_formatter(
                plt.FuncFormatter(lambda v, _p: _format_axis_value(v)))
            ax2.yaxis.set_major_formatter(
                plt.FuncFormatter(lambda v, _p: _format_axis_value(v)))

            # Combined legend — both lines named in one box.
            ax.legend(handles=[line1, line2], loc="best",
                      frameon=False, fontsize=9)

            # Hide the twin axis's top/right spines for a clean look.
            ax2.spines["top"].set_visible(False)
            ax2.spines["right"].set_color("#9aa0a6")
            ax2.spines["right"].set_linewidth(0.8)
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/test_tools/test_viz_renderer.py::test_render_chart_writes_png_for_trend_dual tests/test_tools/test_viz_renderer.py::test_render_chart_trend_dual_returns_none_when_only_one_resolvable_series -v`

Expected: PASS — both tests.

- [ ] **Step 2.5: Run full renderer test file**

Run: `pytest tests/test_tools/test_viz_renderer.py -v`

Expected: all existing tests still pass.

- [ ] **Step 2.6: Stage and prepare commit**

```bash
git add tools/viz_renderer.py tests/test_tools/test_viz_renderer.py
```

Commit message:
```
feat(viz): render trend_dual — twin y-axis for 2 series on different scales
```

---

## Task 3: Renderer — `trend_grid` branch (N stacked panels, shared x)

Add the matplotlib render path for `trend_grid`. `plt.subplots(n, 1, sharex=True)`, one y_field per panel, shared time axis on the bottom panel only. Reuses `_apply_style` per panel.

**Files:**
- Modify: `tools/viz_renderer.py`
- Test: `tests/test_tools/test_viz_renderer.py`

- [ ] **Step 3.1: Write failing renderer tests**

Append to `tests/test_tools/test_viz_renderer.py`:

```python
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
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `pytest tests/test_tools/test_viz_renderer.py::test_render_chart_writes_png_for_trend_grid tests/test_tools/test_viz_renderer.py::test_render_chart_trend_grid_drops_unparseable_series_silently -v`

Expected: FAIL — `trend_grid` falls through to the existing `else: # share` branch, which expects a single-series horizontal bar layout and either raises or produces a wrong-shaped chart.

- [ ] **Step 3.3: Implement the `trend_grid` render branch**

In `tools/viz_renderer.py`, the `trend_grid` branch needs different figure geometry from the other kinds — it needs N subplots instead of one. Replace the *opening* of the render block to handle this:

Currently the render block starts (around line 256):

```python
    try:
        fig, ax = plt.subplots(figsize=(8.5, 4.5), dpi=140)

        if kind == "trend":
```

Change it to:

```python
    try:
        if kind == "trend_grid":
            # One panel per resolved series, vertical stack, shared x-axis.
            n_panels = len(extracted)
            fig, axes = plt.subplots(
                n_panels, 1, sharex=True,
                figsize=(8.5, 2.2 * n_panels), dpi=140,
            )
            # plt.subplots returns a single Axes when n_panels == 1; wrap
            # to a list so the per-panel loop below is uniform.
            if n_panels == 1:
                axes = [axes]

            xs_first = extracted[0][1][0]
            indices = list(range(len(xs_first)))
            n = len(xs_first)
            stride = max(1, n // 10)
            visible_xticklabels = [
                str(xs_first[i]) if (i % stride == 0 or i == n - 1) else ""
                for i in indices
            ]

            for i, (yf, (_, ys)) in enumerate(extracted):
                panel_ax = axes[i]
                color = _PALETTE[i % len(_PALETTE)]
                panel_ax.plot(indices, ys, marker="o", linewidth=2.0,
                              markersize=5.0, color=color)
                panel_ax.set_ylabel(yf)
                panel_ax.yaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _p: _format_axis_value(v)))
                _apply_style(panel_ax, fig)
                # Hide x-tick labels on every panel except the bottom one
                # so the shared x is only labelled once at the foot.
                if i < n_panels - 1:
                    panel_ax.tick_params(labelbottom=False)

            # Bottom panel gets the rotated x-tick labels + axis label.
            bottom_ax = axes[-1]
            bottom_ax.set_xticks(indices)
            bottom_ax.set_xticklabels(visible_xticklabels, rotation=30,
                                      ha="right", fontsize=9)
            bottom_ax.set_xlabel(x_field)

            # Keep panels visually distinct but tight.
            fig.tight_layout(h_pad=0.6)
            fig.savefig(out_path, format="png", bbox_inches="tight",
                        facecolor=fig.get_facecolor())
        else:
            fig, ax = plt.subplots(figsize=(8.5, 4.5), dpi=140)

            if kind == "trend":
```

Now find the end of the existing render-block (after the `else: # share` block's `ax.set_xlim(...)` line, before `_apply_style(ax, fig)` is called and the figure is saved). The existing tail looks like:

```python
        _apply_style(ax, fig)
        fig.tight_layout()
        fig.savefig(out_path, format="png", bbox_inches="tight",
                    facecolor=fig.get_facecolor())
    except Exception as exc:  # noqa: BLE001
```

That tail is now only reachable from the `else:` branch (the single-axes path), which is correct — `trend_grid` already saved inside its own block above. No change needed here.

Note on the failure path: the existing `except Exception` and `finally` blocks reference `fig` — that variable is bound in both branches now, so `plt.close(fig)` continues to work.

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `pytest tests/test_tools/test_viz_renderer.py::test_render_chart_writes_png_for_trend_grid tests/test_tools/test_viz_renderer.py::test_render_chart_trend_grid_drops_unparseable_series_silently -v`

Expected: PASS — both tests.

- [ ] **Step 3.5: Run full renderer test file**

Run: `pytest tests/test_tools/test_viz_renderer.py -v`

Expected: all existing tests still pass — the only change to the single-axes path was moving its `fig, ax = plt.subplots(...)` into an `else` branch, preserving identical behavior.

- [ ] **Step 3.6: Stage and prepare commit**

```bash
git add tools/viz_renderer.py tests/test_tools/test_viz_renderer.py
```

Commit message:
```
feat(viz): render trend_grid — N stacked panels with shared x-axis
```

---

## Task 4: Vega-Lite specs for `trend_dual` and `trend_grid`

Extend `kp_to_vega_spec` so downstream interactive renderers can reproduce the new layouts. `trend_dual` → layered spec with `resolve.scale.y: independent`. `trend_grid` → `vconcat` of N single-series specs sharing x.

**Files:**
- Modify: `tools/viz_renderer.py`
- Test: `tests/test_tools/test_viz_renderer.py`

- [ ] **Step 4.1: Write failing spec tests**

Append to `tests/test_tools/test_viz_renderer.py`:

```python
def test_kp_to_vega_spec_emits_layered_independent_y_for_trend_dual():
    """trend_dual → Vega-Lite `layer` of two line marks with independent
    y scales so each series uses its own y range."""
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
    # Two layers — one per series — each with its own y encoding.
    assert isinstance(spec.get("layer"), list)
    assert len(spec["layer"]) == 2
    assert spec["layer"][0]["mark"] == "line"
    assert spec["layer"][0]["encoding"]["y"]["field"] == "score"
    assert spec["layer"][1]["encoding"]["y"]["field"] == "dpd"
    # Independent y scales — this is what makes the layout dual-axis.
    assert spec["resolve"]["scale"]["y"] == "independent"
    # JSON-roundtrippable.
    assert json.loads(json.dumps(spec)) == spec


def test_kp_to_vega_spec_emits_vconcat_for_trend_grid():
    """trend_grid → Vega-Lite `vconcat` of N single-series line specs."""
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
    # Each sub-spec is a line chart of one y_field against the shared x.
    fields = [sub["encoding"]["y"]["field"] for sub in spec["vconcat"]]
    assert fields == ["tsr", "cdss", "txn_count"]
    for sub in spec["vconcat"]:
        assert sub["mark"] == "line"
        assert sub["encoding"]["x"]["field"] == "period"
    assert json.loads(json.dumps(spec)) == spec
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `pytest tests/test_tools/test_viz_renderer.py::test_kp_to_vega_spec_emits_layered_independent_y_for_trend_dual tests/test_tools/test_viz_renderer.py::test_kp_to_vega_spec_emits_vconcat_for_trend_grid -v`

Expected: FAIL — the current `kp_to_vega_spec` only handles `trend` / `bar` / `share`; the new kinds fall through and produce nothing matching the assertions.

- [ ] **Step 4.3: Add `trend_dual` and `trend_grid` paths to `kp_to_vega_spec`**

In `tools/viz_renderer.py`, find `kp_to_vega_spec` and locate the if/elif chain that dispatches by `kind` (currently `if kind == "trend"` → `elif kind == "bar"` → `else:  # share`). Insert two new branches before the `else: # share` line so the order is `trend` → `bar` → `trend_dual` → `trend_grid` → `share`:

```python
    elif kind == "trend_dual":
        # Layered spec — two line marks, each bound to its own y_field,
        # with independent y scales so the two series don't have to share
        # a numeric range.
        y_left, y_right = y_fields[0], y_fields[1]
        spec["layer"] = [
            {
                "mark": "line",
                "encoding": {
                    "x": {"field": x_field, "type": "ordinal"},
                    "y": {"field": y_left, "type": "quantitative"},
                    "color": {"datum": y_left, "type": "nominal"},
                },
            },
            {
                "mark": "line",
                "encoding": {
                    "x": {"field": x_field, "type": "ordinal"},
                    "y": {"field": y_right, "type": "quantitative"},
                    "color": {"datum": y_right, "type": "nominal"},
                },
            },
        ]
        spec["resolve"] = {"scale": {"y": "independent"}}
    elif kind == "trend_grid":
        # Stacked single-series line charts sharing the x-axis. Each
        # sub-spec is independent so each y-scale auto-fits its series.
        spec["vconcat"] = [
            {
                "mark": "line",
                "encoding": {
                    "x": {"field": x_field, "type": "ordinal"},
                    "y": {"field": yf, "type": "quantitative"},
                },
            }
            for yf in y_fields
        ]
```

Note: the assignments to `spec["encoding"]` in the original `trend` / `bar` branches don't apply to these new kinds — `trend_dual` builds layered encodings, and `trend_grid` puts encodings on each `vconcat` sub-spec.

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `pytest tests/test_tools/test_viz_renderer.py::test_kp_to_vega_spec_emits_layered_independent_y_for_trend_dual tests/test_tools/test_viz_renderer.py::test_kp_to_vega_spec_emits_vconcat_for_trend_grid -v`

Expected: PASS.

- [ ] **Step 4.5: Run the full renderer + viz tool test files**

Run: `pytest tests/test_tools/test_viz_renderer.py tests/test_tools/test_data_viz_tools.py -v`

Expected: all tests pass — `kp_to_vega_spec` still produces the same shape for `trend` / `bar` / `share`.

- [ ] **Step 4.6: Stage and prepare commit**

```bash
git add tools/viz_renderer.py tests/test_tools/test_viz_renderer.py
```

Commit message:
```
feat(viz): Vega-Lite specs for trend_dual (layered) and trend_grid (vconcat)
```

---

## Task 5: End-to-end happy-path tests through `make_chart`

Confirm the full pipeline — function-tool call → validator → renderer → KP persistence — works for both new kinds. These tests don't introduce new code; they exist to verify the validator (Task 1) + renderer (Tasks 2–3) + spec emitter (Task 4) work together when invoked through `make_chart` the way a specialist would call it.

**Files:**
- Test: `tests/test_tools/test_data_viz_tools.py`

- [ ] **Step 5.1: Write end-to-end tests**

Append to `tests/test_tools/test_data_viz_tools.py`:

```python
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
```

- [ ] **Step 5.2: Run tests to verify they pass**

Run: `pytest tests/test_tools/test_data_viz_tools.py::test_make_chart_trend_dual_end_to_end tests/test_tools/test_data_viz_tools.py::test_make_chart_trend_grid_end_to_end -v`

Expected: PASS — all pieces from Tasks 1–4 wired correctly.

- [ ] **Step 5.3: Run the full tools test suite**

Run: `pytest tests/test_tools/ -v`

Expected: all tests pass; no regressions in adjacent suites.

- [ ] **Step 5.4: Stage and prepare commit**

```bash
git add tests/test_tools/test_data_viz_tools.py
```

Commit message:
```
test(viz): end-to-end happy-path coverage for trend_dual and trend_grid
```

---

## Task 6: Update `make_chart` tool description so the LLM sees the new kinds

The tool's `description_override` is part of the model-visible function-tool signature. It currently mentions only multi-series `trend`. Extend it to name the three modes briefly so the model has the new vocabulary at decision time (the full decision rule lives in the skill — Task 7).

**Files:**
- Modify: `tools/data_viz_tools.py`
- Test: none (description text isn't asserted on; the skill-level change is what teaches the model).

- [ ] **Step 6.1: Update `description_override` in `build_make_chart_tool`**

In `tools/data_viz_tools.py`, find the `description_override=(...)` argument inside `build_make_chart_tool` (currently around lines 41–50). Replace it with:

```python
        description_override=(
            "Render a chart from a series of points and surface it in the "
            "reasoning trace (NOT inline in the chat answer). Use AFTER a "
            "data tool (summarize_trend / summarize_by_group / "
            "aggregate_column) produced the numbers; pass that series via "
            "`points`. Multiple variables on the same x-axis (typically "
            "time) belong on ONE chart — pick the kind by scale: `trend` "
            "for same-scale series on a shared y-axis; `trend_dual` for "
            "exactly 2 series on different but related scales (twin y); "
            "`trend_grid` for 3+ series on different scales (N stacked "
            "panels). Be selective — only chart when the visual conveys "
            "what numbers alone can't."
        ),
```

- [ ] **Step 6.2: Run the full tools test suite — description is part of the function-tool surface, so an unintended formatting break would show up here**

Run: `pytest tests/test_tools/ -v`

Expected: all tests pass.

- [ ] **Step 6.3: Stage and prepare commit**

```bash
git add tools/data_viz_tools.py
```

Commit message:
```
docs(viz): expand make_chart description with the three trend modes
```

---

## Task 7: Update `data_query.md` skill with the decision rule

The skill is what teaches the specialist *when* to use each kind. Replace the existing multi-series paragraph with the three-mode decision rule and a note about using `get_table_schema` to tell same-scale from different-scale.

**Files:**
- Modify: `skills/workflow/data_query.md`
- Test: none (skill text is prose).

- [ ] **Step 7.1: Replace the charting decision paragraph**

In `skills/workflow/data_query.md`, find the existing paragraph that begins:

```
When you DO chart, **combine related series into ONE multi-series chart**, not N single-line ones — `y_fields=["spend", "payment"]` for spend-vs-payment per month is one chart, not two.
```

and the multi-table merging paragraph that follows it (the one beginning with `**You can — and often should — merge data from MULTIPLE tables / tool calls into one chart.**`, ending with the `make_chart(..., y_fields=["spend", "payment"], ...)` code block and the paragraph that begins `Same pattern for: cleared vs returned payments...`).

Replace both paragraphs with:

```
When multiple variables share the same x-axis (typically time), they belong on ONE chart, not N. Pick the kind by scale:

- **Same scale and unit** (all percentages, all dollar amounts, all score bands): `kind="trend"` with `y_fields=[var1, var2, ...]` — single shared y-axis, one line per variable.
- **Exactly 2 variables on different but related scales** (e.g., a score 0–1000 + DPD 0–90, or a count + a rate): `kind="trend_dual"` with `y_fields=[primary, secondary]` — twin y-axes, primary on the left, secondary on the right.
- **3+ variables of different scales** (TSR + CDSS + counts + dollars, etc.): `kind="trend_grid"` with `y_fields=[var1, var2, var3, ...]` — N stacked panels sharing the time axis, each with its own y-scale (cap at 6).

Never emit N separate `kind="trend"` calls for variables on the same x-axis. One merged points list, one make_chart call. Tell same-scale from different-scale by checking the source columns' units via `get_table_schema` before choosing.

**You can — and often should — merge data from MULTIPLE tables / tool calls into one chart.** A two-line spend-vs-payment chart usually comes from:

1. `summarize_trend('spends', 'Amount', 'Date', period='month', op='sum')` → spend series.
2. `summarize_trend('payments', 'Payment Amount', 'Date', period='month', op='sum', filter_column='payment_status', filter_value='success')` → cleared-payment series.
3. Merge the two series by month into one points list, then ONE `make_chart` call:

```
points = [{"period": "2024-11", "spend": 300, "payment": 280},
          {"period": "2024-12", "spend": 500, "payment": 420}, ...]
make_chart(..., y_fields=["spend", "payment"], ...)
```

Same pattern for: cleared vs returned payments, internal vs external delinquency index over time, top-3 merchants' trends merged by period. The shared `x_field` (typically `period` or `group`) is what makes the merge possible. One informative multi-line chart beats three single-line charts that the reviewer has to mentally align.
```

(The multi-table-merging worked example is kept verbatim, but moved to AFTER the decision rule so the rule comes first.)

- [ ] **Step 7.2: Stage and prepare commit**

```bash
git add skills/workflow/data_query.md
```

Commit message:
```
docs(skills): add chart-kind decision rule for same-scale vs different-scale variables
```

---

## Final verification

- [ ] **Step F.1: Run the full project test suite to catch any cross-suite regression**

Run: `pytest tests/ -v`

Expected: all green. If anything outside `tests/test_tools/` fails, the change touched something downstream — investigate the failure before claiming completion.

- [ ] **Step F.2: Manual visual check on each new kind**

Render a sample of each kind once and open the PNGs — `trend_dual` should show two y-axes with color-matched labels, and `trend_grid` should show N stacked panels with x-tick labels only on the bottom. This is not automated; visual quality is what's being verified.

Quick script (run from repo root):

```bash
python -c "
from pathlib import Path
from tools.viz_renderer import render_chart

out_dir = Path('reports/_viz_smoketest')
out_dir.mkdir(parents=True, exist_ok=True)

dual = render_chart({
    'topic': 'smoketest_dual',
    'viz': {'kind': 'trend_dual', 'x_field': 'period',
            'y_fields': ['score', 'dpd']},
    'numbers': [
        {'period': '2024-11', 'score': 720, 'dpd': 0},
        {'period': '2024-12', 'score': 705, 'dpd': 15},
        {'period': '2025-01', 'score': 690, 'dpd': 30},
        {'period': '2025-02', 'score': 680, 'dpd': 45},
    ],
}, out_dir, turn_id='smoketest')

grid = render_chart({
    'topic': 'smoketest_grid',
    'viz': {'kind': 'trend_grid', 'x_field': 'period',
            'y_fields': ['tsr', 'cdss', 'txn_count']},
    'numbers': [
        {'period': '2024-11', 'tsr': 720, 'cdss': 680, 'txn_count': 42},
        {'period': '2024-12', 'tsr': 705, 'cdss': 665, 'txn_count': 38},
        {'period': '2025-01', 'tsr': 690, 'cdss': 650, 'txn_count': 35},
        {'period': '2025-02', 'tsr': 680, 'cdss': 640, 'txn_count': 31},
    ],
}, out_dir, turn_id='smoketest')

print('dual →', dual)
print('grid →', grid)
"
```

Open `reports/_viz_smoketest/smoketest-smoketest_dual.png` and `reports/_viz_smoketest/smoketest-smoketest_grid.png` and confirm the layouts visually. Delete the directory after inspection.

- [ ] **Step F.3: Confirm with the user before opening a PR / merging**

Per project memory, do not push or open a PR without explicit instruction.

---

## Plan self-review (writer's checklist — already run)

- **Spec coverage:** validator (Task 1), trend_dual renderer (Task 2), trend_grid renderer (Task 3), Vega specs (Task 4), tool description (Task 6), skill prompt (Task 7), end-to-end glue (Task 5). All five spec components covered; "out of scope" items (auto-detection, normalization, bar/share changes, server-side merging) are not implemented and not snuck in.
- **Placeholders:** none — every step has exact code, exact files, exact commands.
- **Type / name consistency:** `_VALID_KINDS` (tool) and `_SUPPORTED_KINDS` (renderer) both extended to the same five values. `y_fields` is the list-typed key everywhere. `extracted` is the list-of-(yf, (xs, ys)) shape consistent with the existing render branches.
- **TDD discipline:** every renderer / validator task writes the failing test first, then the implementation, then re-runs the test. Tasks 5 and F-steps are verification only and explicitly noted as such.
