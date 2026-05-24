---
name: feedback-plots-preference
description: Plot defaults — horizontal bars for category breakdowns; meaningful sort order; uniform time intervals for unwindowed questions.
metadata:
  type: feedback
---

The user has stated three opinionated defaults for any chart produced by the system (matplotlib PNG, Vega-Lite SVG, or markdown table card):

1. **Prefer horizontal bar plots** for category breakdowns. `kind="share"` is the default — long category names (merchant names, industry labels) read naturally as y-axis labels. The auto-chart system uses `kind="share"` for `summarize_by_group` results.

2. **Order points with logic, not tool-return order.** Every chart's points list should be sorted by a meaningful dimension before rendering:
   - **Time-series** (`trend`, `trend_dual`, `trend_grid`): chronological ascending by the x-axis date/period.
   - **Ranking / top-N** (`bar`, `share`, breakdowns): by the value column descending (biggest on the left for `bar`, biggest at the top for `share`).
   - **Category-natural** (status enums like `success` / `return`, severity bands): match the column's documented ordering or domain semantics, not alphabetical.

   "Tool-return order" — whichever order `summarize_by_group` / `query_table` happened to scan rows — is NEVER acceptable on a published chart. The reviewer reads charts as a visual ordering claim; arbitrary order misleads.

3. **Uniform time intervals for unwindowed questions.** When the reviewer asks a trajectory question WITHOUT specifying a time window (e.g., "how did TSR react?" not "how did TSR react in Q4 2024?"), the chart should show the FULL data range with uniform monthly intervals. Don't skip periods with no data — show them as gaps so the reviewer sees the data coverage. Don't cluster points around anchor periods the claim mentions.

**Why:** The user has reviewed many charts in iteration and consistently flags: (a) unsorted bars hide the headline finding, (b) horizontal bars with readable labels are preferred for category breakdowns (updated 2026-05-25, originally preferred vertical — walked back after seeing long category names), (c) non-uniform time axes mislead about the data's temporal density.

**How to apply:**

- **Auto-chart** (`redacting_tool._auto_chart_from_tool_outputs`): uses `kind="share"` (horizontal) for group breakdowns. Trend series preserve the full period range from the tool output.
- **`tools/viz_renderer.py`**: `_sort_points` handles temporal / numeric / ranking / alphabetic — temporal ascending and ranking descending are the primary branches.
- **`skills/workflow/data_viz.md`**: kind-picking table uses `share` for categorical breakdowns, `trend`/`trend_dual` for time-series.
- **Time intervals**: `summarize_trend` returns all periods in the data range. The auto-chart passes these through unchanged — no period filtering or selection. The renderer plots them at uniform intervals on the x-axis.

**Cross-references:** [[feedback-orchestration-flow-ux]], `data_viz.md` skill, `_auto_chart_from_tool_outputs` in `redacting_tool.py`.
