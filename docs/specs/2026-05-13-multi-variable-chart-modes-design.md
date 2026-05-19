# Multi-variable chart modes — `trend_dual` and `trend_grid`

**Date:** 2026-05-13
**Status:** Design approved; ready for implementation plan.

## Problem

Specialists currently emit one `make_chart(kind="trend", ...)` call per variable when investigating multiple metrics over the same time window (e.g., TSR, CDSS, and other risk scores for a single case). The reviewer ends up with N stand-alone single-line charts to mentally align — exactly what `data_query.md`'s "combine related series into ONE multi-series chart" rule is meant to prevent.

The existing `trend` kind already supports multi-series via `y_fields: list[str]`, but it plots every series on a shared y-axis. That works when the variables share a scale (all percentages, all dollar amounts) and fails when they don't: a 0–1000 credit score and 0–90 DPD on the same y-axis compress one signal into the floor. The specialist's escape hatch today is to revert to N separate charts.

Result: same-scale and different-scale cases need different layouts, but the tool only offers one — so the specialist routinely takes the worst path (N separate charts) instead of the right path.

## Design

Add two new `kind` values to `make_chart`. The specialist picks based on the variables it's plotting; the skill teaches the decision rule.

| `kind` | When to use | Layout | Series count |
|---|---|---|---|
| `trend` *(existing)* | All y_fields share the same scale/unit | Multi-line on a single shared y-axis | ≥ 1 |
| `trend_dual` *(new)* | Exactly 2 y_fields on different but related scales (e.g., score + DPD, count + rate) | One panel, two y-axes (left = first y_field, right = second) | exactly 2 |
| `trend_grid` *(new)* | 3+ y_fields on different scales, OR 2 y_fields where one dwarfs the other | N stacked subplots, shared x-axis, per-panel y-scale | 2–6 |

Single `make_chart` call regardless of mode. Single merged `points` list — same `x_field` for every series. Single PNG output. The auto-distiller / KB / chart-collection path downstream is unchanged.

## Components

### 1. `tools/data_viz_tools.py`

- Extend `_VALID_KINDS` to `("trend", "bar", "share", "trend_dual", "trend_grid")`.
- Update the tool's `description_override` to mention all three trend modes briefly.
- Validation additions:
  - `kind == "trend_dual"` → require exactly 2 entries in `y_fields`. Error string tells the model to use `trend_grid` if it has 3+ series.
  - `kind == "trend_grid"` → require 2 ≤ len(y_fields) ≤ 6. Error string tells the model to drop low-signal series if it has 7+.
  - Existing `share` single-series rule unchanged.

### 2. `tools/viz_renderer.py`

Two new render branches in `render_chart`. Both reuse `_resolve_axes`, `_extract_xy`, `_PALETTE`, `_apply_style`, and `_format_axis_value`.

**`trend_dual`** — single subplot with twin y-axes:
- Primary line plotted on `ax` using `_PALETTE[0]`; y-label = first y_field, label color matched to line.
- Secondary line plotted on `ax.twinx()` using `_PALETTE[1]`; y-label = second y_field, label color matched.
- Shared x-axis (same `set_xticks` / `set_xticklabels` logic as existing `trend`).
- Single legend combining both lines (`ax.legend(lines_primary + lines_secondary, labels, ...)`).
- `_apply_style` applied to the primary axis; the twin axis gets minimal styling (no extra gridlines — would clash with primary's).

**`trend_grid`** — `plt.subplots(n, 1, sharex=True, figsize=(8.5, 2.2 * n), dpi=140)`:
- One y_field per panel, in `y_fields` order.
- Each panel: one line in `_PALETTE[i % len(_PALETTE)]`, y-label = field name, `_format_axis_value` on the y-axis.
- x-tick labels rendered only on the bottom panel (others get empty tick labels via `ax.tick_params(labelbottom=False)`).
- `_apply_style` applied per panel.
- `fig.tight_layout()` with a small `h_pad` to keep panels close but distinct.

Failure modes (axes_unresolved, y values non-numeric, matplotlib error) flow through the existing logging + `return None` path — caller behavior unchanged.

### 3. `kp_to_vega_spec`

- `trend_dual` → layered spec with two `mark: "line"` layers, each binding its own y_field, plus `resolve: { scale: { y: "independent" } }` so the two y-axes scale separately. Color encoding distinguishes the layers in the legend.
- `trend_grid` → `vconcat` of N single-line specs, each with `mark: "line"`, sharing the x encoding. Title carried on the top spec.
- Existing `trend` / `bar` / `share` spec paths untouched.

### 4. `skills/workflow/data_query.md` — § Charting

Replace the current "When you DO chart, combine related series into ONE multi-series chart…" paragraph with a decision rule:

> When multiple variables share the same x-axis (typically time), they belong on ONE chart, not N. Pick the kind by scale:
> - **Same scale and unit** (all percentages, all dollar amounts, all score bands): `kind="trend"` with `y_fields=[var1, var2, ...]`.
> - **Exactly 2 variables on different but related scales** (e.g., a score 0–1000 + DPD 0–90, or a count + a rate): `kind="trend_dual"` — `y_fields=[primary, secondary]` plots primary on the left axis, secondary on the right.
> - **3+ variables of different scales** (TSR + CDSS + counts + dollars, etc.): `kind="trend_grid"` — N stacked panels share the time axis, each with its own y-scale.
>
> Never emit N separate `kind="trend"` calls for variables on the same x-axis. One merged points list, one make_chart call. Check the source columns' units via `get_table_schema` before choosing — that's where you tell same-scale from different-scale.

Keep the existing "shape is load-bearing / ≥ 4 points / one call" guardrails. Keep the multi-table merging example (spend vs payment) as the canonical `trend` illustration.

### 5. Tests

- `tests/test_tools/test_data_viz_tools.py`:
  - `trend_dual` rejects 1 y_field with a message mentioning `trend`.
  - `trend_dual` rejects 3 y_fields with a message mentioning `trend_grid`.
  - `trend_grid` rejects 1 y_field.
  - `trend_grid` rejects 7 y_fields.
  - Valid `trend_dual` / `trend_grid` calls return `[chart created] …` and write to the KB (use the existing fake-context fixture).
- `tests/test_tools/test_viz_renderer.py` (if it exists; otherwise extend wherever the renderer is currently tested):
  - `render_chart` produces a PNG for `trend_dual` with 2 series of mismatched scale.
  - `render_chart` produces a PNG for `trend_grid` with 3 series of mismatched scale.
  - `kp_to_vega_spec` for `trend_dual` includes `resolve.scale.y == "independent"`.
  - `kp_to_vega_spec` for `trend_grid` includes a `vconcat` array of length N.

## Out of scope

- **Auto-detecting `kind` server-side.** The specialist already has the units and ranges from `get_table_schema`; pushing the decision into the tool replaces explicit selection with a heuristic.
- **Per-series normalization** (z-score / index-to-100). Risk reviewers want absolute values; normalization hides them. Additive flag if requested later, not a layout choice now.
- **`bar` / `share` changes.** Untouched.
- **Auto-merging concurrent `make_chart` calls within one turn.** The skill prompt change forces the specialist to merge upstream; no need for server-side combination.

## Risks / open questions

- **Legend crowding on `trend_dual`.** Combining lines from `ax` and `ax.twinx()` into one legend is matplotlib-idiomatic but adds a few lines vs. the existing `trend` legend. Acceptable; verify visually during implementation.
- **Vertical space on `trend_grid` at 5–6 panels.** `figsize=(8.5, 2.2 * n)` → 13.2" tall at n=6. Lightbox display in the UI scales the image; ≤ 6 stays readable. The validator caps at 6.
- **Twin-axis convention.** Picking left = first y_field is convention-driven, not enforced. Skill prompt names this so the specialist orders `y_fields` deliberately (primary signal left).
