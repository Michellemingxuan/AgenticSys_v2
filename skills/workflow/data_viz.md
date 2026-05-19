---
name: Data Viz
description: Shared chart-construction rules — when to call `make_chart` vs. trust the auto-distiller; how to pick the right kind; multi-series alignment; threshold reference lines.
type: workflow
owner: [base_specialist, general_specialist]
mode: inline
tools: [make_chart]
---

Centralized rules for any agent that may call `make_chart`. Composed into the specialist instructions (after `data_query.md`) and the general specialist instructions (after `comparison.md`) so the chart rules don't drift between callers.

## Default: trust the auto-distiller

Don't call `make_chart` for routine findings. The auto-distiller post-processes your `findings` after the answer returns and renders the chartable claims automatically. Calling `make_chart` yourself costs an extra LLM round-trip (~3-6s) on the critical path.

Call `make_chart` **only when ALL hold**:
1. The visual conveys what numbers alone can't — slope, peak, divergence, threshold breach over time.
2. You're merging data from MULTIPLE tool calls / tables into one chart the distiller can't reconstruct (e.g. spend trend + payment trend on one chart, or `modeling`'s indicator + `spend_payments`'s returned-payments).
3. The reviewer explicitly asked for a chart/table.

Hard cap: **1-2 charts per turn**. Skip charting for single scalars, qualitative findings, and data-gap reports.

## Pick the kind

| Kind | When | y_fields shape |
|---|---|---|
| `trend` | Time series, ONE metric (or 2+ metrics on the SAME scale + unit — all percentages, all dollar amounts) | `[var]` or `[var1, var2, ...]` |
| `trend_dual` | EXACTLY 2 time-series metrics on DIFFERENT but related scales (e.g. CDSS 0-1 probability + TSR 0-100 score, or a count + a rate) | `[primary, secondary]` — primary on the left axis, secondary on the right |
| `trend_grid` | 3-6 time-series metrics with DIFFERENT scales (TSR + CDSS + counts + dollars, etc.) | `[var1, var2, ..., varN]` — N stacked panels sharing the time axis |
| `bar` | Categorical x with ≤ 4 categories and short x-labels | single or multi |
| `share` | Categorical x with **≥ 5 categories** (top-N merchants / industries / branches), single-series only — horizontal layout sorted by value avoids rotated-label overlap | `[var]` |
| `table` | 1-3 rows — surfaces as a table card in the Plots panel, no image rendered | any |

**Quick decision tree for time-series questions:**

```
N metrics on same x-axis?
├── 1 metric → trend
└── 2+ metrics:
    ├── same scale + unit  → trend (multi-series, one shared y-axis)
    ├── exactly 2 metrics, different scales → trend_dual (twin y-axis)
    └── 3-6 metrics, different scales       → trend_grid (stacked panels)
```

Tell same-scale from different-scale by checking the source columns' units via `get_table_schema` before choosing. Plot kinds require **≥ 4 points**; table kind has no minimum.

## Multi-series alignment (load-bearing)

When 2+ metrics share an x-axis, emit **ONE** `make_chart` call with `y_fields=[var1, var2, ...]`. **Never** emit N separate `kind="trend"` calls for variables on the same x — one informative multi-line chart beats N single-line charts the reviewer has to mentally align.

Merge data from MULTIPLE tool calls into one `points` list by aligning on a shared `x_field` (typically `period` or `group`):

```
points = [
  {"period": "2024-11", "credit_loss_prob": 0.12, "tot_struct_risk_score": 22.0},
  {"period": "2024-12", "credit_loss_prob": 0.18, "tot_struct_risk_score": 25.4},
  ... ALL rows from BOTH underlying aggregates ...
]
make_chart(topic='cdss_tsr_trajectory', kind='trend_dual', ...,
           y_fields=['credit_loss_prob', 'tot_struct_risk_score'], ...)
```

**Pass EVERY row from the underlying aggregates in `points` — not just the periods/groups you mention in the claim.** The renderer plots `points` exactly as given; dropping interior rows produces a chart with gaps that misrepresents the data and contradicts the claim's stated time window.

## Threshold reference lines

When the catalog description for a metric named a risky threshold (e.g. *"Values above 0.5 are risky"* on `credit_loss_prob`, *"Scores from 20-100 are considered risky"* on `tot_struct_risk_score`), carry that threshold on every `points` row so the renderer draws a horizontal dashed reference line at that y. Two key shapes by chart kind:

- **Single-series `trend`** — use the bare `threshold` key on every row:
  ```
  [{"period": "2024-Q4", "value": 3, "threshold": 1}, ...]
  ```
- **Multi-series `trend` / `trend_dual` / `trend_grid`** (different y_field per series, each with its own cutoff) — use **per-axis** keys named `threshold_<y_field>`, one per axis:
  ```
  [{"period": "2024-11",
    "credit_loss_prob": 0.12, "tot_struct_risk_score": 22.0,
    "threshold_credit_loss_prob": 0.5,
    "threshold_tot_struct_risk_score": 20},
   ...]
  ```

The renderer requires the threshold to be **consistent across every row** — if rows disagree or any row is missing the key, no line is drawn (a partial threshold is worse than none, since a missing-on-some-rows would imply a step function the data doesn't support).

## Naming: one topic, one concept

Two KPs in the same turn MUST NOT share a `topic` slug unless they answer the same conceptual question. Downstream chart collection dedupes by `(specialist, topic)`, so collisions cause charts to silently disappear (real failure: case-aefd66 turn `5b8f94089581` — both CDSS and TSR landed under `model_scores_trend` and the second render overwrote the first).

- Different metrics → different topics: `cdss_score_trend`, `tsr_score_trend` (NOT both `model_scores_trend`).
- One conceptual question covering multiple metrics → ONE multi-series KP with a descriptive topic (`cdss_tsr_trajectory`).

When the claim is about a specific named metric / indicator, put the metric name **IN** the topic slug — never a generic family label.

## Returns

`make_chart` returns `[chart created] …` on success, `[make_chart error] …` with a structured reason on validation failures (wrong `kind`, points < 4 for plot kinds, mismatched `y_fields` length, etc.). Read the error and adjust before re-calling — don't retry the same call expecting a different result.
