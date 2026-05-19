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
| `trend_dual` | EXACTLY 2 time-series metrics on DIFFERENT but related scales (e.g. a score 0-1000 + DPD 0-90, or a count + a rate). NOTE: CDSS and TSR are BOTH 0-100 scores — use `trend` (single shared y-axis), not `trend_dual`. | `[primary, secondary]` — primary on the left axis, secondary on the right |
| `trend_grid` | 3-6 time-series metrics with DIFFERENT scales (TSR + CDSS + counts + dollars, etc.) | `[var1, var2, ..., varN]` — N stacked panels sharing the time axis |
| `bar` | **DEFAULT for categorical breakdowns.** Vertical bars, ranked by value descending. Use this for top-N merchants / industries / branches / specialists / any categorical ranking regardless of category count. The renderer auto-sorts by value desc and rotates x-tick labels when they'd overlap. | single or multi |
| `share` | **Escape valve ONLY** — use when category labels are SO LONG (multi-word merchant names, "AMAZON DIGITAL DOWNLOADS NORTHEAST ▸▸▸") AND there are many (8+) of them that vertical rotated labels become illegible. The bar layout is identical in information; only the orientation flips. **Do not pick `share` based on count alone** — count alone is not a reason. | `[var]` |
| `table` | 1-3 rows — surfaces as a table card in the Plots panel, no image rendered | any |

**Rule of thumb for `bar` vs `share`**: prefer **vertical `bar`** by default. Reviewers read time-then-rank flow left-to-right; a vertical ranked bar plot reads "biggest on the left, smallest on the right" intuitively. Switch to `share` only if you've genuinely tried `bar` mentally and the labels can't fit. **Count alone (≥5, ≥8, etc.) is NOT a reason to switch.** See `.claude/memory/feedback_plots_preference.md` — this is a stated user preference, not a heuristic.

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

When the catalog description for a metric named a risky threshold (e.g. *"Scores from 10-100 are considered risky"* on `credit_loss_prob` → threshold = 10; *"Scores from 20-100 are considered risky"* on `tot_struct_risk_score` → threshold = 20; *"Values above 0.5 are risky"* on a probability column → threshold = 0.5), carry that threshold on every `points` row so the renderer draws a horizontal dashed reference line at that y. The threshold value MUST come from the column's catalog description (visible via `get_table_schema`) — never from memory or a generic guess.

- **Single-series `trend`** — use the bare `threshold` key on every row:
  ```
  [{"period": "2024-Q4", "value": 3, "threshold": 1}, ...]
  ```
- **Multi-series `trend` / `trend_dual` / `trend_grid`** (different y_field per series, each with its own cutoff) — use **per-axis** keys named `threshold_<y_field>`, one per axis. Real numbers must match `get_table_schema` output for THIS case:
  ```
  [{"period": "2024-11",
    "credit_loss_prob": 12, "tot_struct_risk_score": 22.0,
    "threshold_credit_loss_prob": 10,
    "threshold_tot_struct_risk_score": 20},
   ...]
  ```
  Here CDSS = 10 and TSR = 20 because the model_scores catalog says *"Scores from 10-100 are considered risky"* for `credit_loss_prob` and *"Scores from 20-100 are considered risky"* for `tot_struct_risk_score`. Re-read those descriptions for the current case before filling in thresholds — never hardcode.

The renderer requires the threshold to be **consistent across every row** — if rows disagree or any row is missing the key, no line is drawn (a partial threshold is worse than none, since a missing-on-some-rows would imply a step function the data doesn't support).

## Naming: one topic, one concept

Two KPs in the same turn MUST NOT share a `topic` slug unless they answer the same conceptual question. Downstream chart collection dedupes by `(specialist, topic)`, so collisions cause charts to silently disappear (real failure: case-aefd66 turn `5b8f94089581` — both CDSS and TSR landed under `model_scores_trend` and the second render overwrote the first).

- Different metrics → different topics: `cdss_score_trend`, `tsr_score_trend` (NOT both `model_scores_trend`).
- One conceptual question covering multiple metrics → ONE multi-series KP with a descriptive topic (`cdss_tsr_trajectory`).

When the claim is about a specific named metric / indicator, put the metric name **IN** the topic slug — never a generic family label.

## Returns

`make_chart` returns `[chart created] …` on success, `[make_chart error] …` with a structured reason on validation failures (wrong `kind`, points < 4 for plot kinds, mismatched `y_fields` length, etc.). Read the error and adjust before re-calling — don't retry the same call expecting a different result.
