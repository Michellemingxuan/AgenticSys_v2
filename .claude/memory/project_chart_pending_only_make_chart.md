---
name: project-chart-pending-only-make-chart
description: chart_pending fires per-specialist DURING a turn from BOTH chart paths; the real `chart` events land at end-of-turn AFTER cross-specialist dedup, so any deduped-away chart orphans its placeholder unless `chart_cancelled` retracts it.
metadata:
  type: project
---

Two chart-producing paths, and **both now emit `chart_pending`** (this file previously said only `make_chart` did — that stopped being true when the emit was added to `_render_auto_charts`, and the staleness is what made the orphaned-placeholder bug hard to see):

1. **`make_chart` tool** (`tools/data_viz_tools.py`) — explicit specialist call. Rare in practice.
2. **Auto-chart** (`agent_factories/agent_tools/auto_chart.py::_render_auto_charts`) — data-driven from `summarize_trend` / `batch_summarize_trend` / `summarize_by_group`. **This is the path that actually runs.** Tell-tale in the JSONL: `auto_chart_rendered` + `viz_rendered`, no `make_chart`.

## The lifecycle gap

`chart_pending` fires **per specialist, during the turn** — the auto-chart task is `create_task`'d the moment a specialist returns, so it is early and precise, keyed `(specialist, topic)`.

The real `chart` events are emitted **at end of turn** in `conductor._finalize`, from `_collect_turn_charts`, which **dedups identical figures across specialists** (`_chart_signature`, first-writer-wins).

So a chart that dedup drops has already had its placeholder announced, and the frontend clears placeholders on a matching `chart` event that now never arrives. The card hangs in the loading state forever, next to the real one — on screen this reads as *"two specialists drew the same plot"*. The frontend cannot fix this alone: nothing in the stream says the pending chart was superseded.

**Resolved** by `chart_cancelled` — `_finalize` diffs `ctx._charts_pending` (populated via `auto_chart.record_chart_pending`) against what it actually emitted and retracts the difference. Additive/backward-compatible: a client that ignores the event behaves as before. **The frontend must handle `chart_cancelled` (drop the placeholder for that `(specialist, topic)`) or the stuck card remains** — see [[feedback-wire-frontend-with-backend]].

## Upstream cause, also fixed

The usual reason two specialists produce the same figure is a **cross-domain peek that got charted**: `bureau` trended `credit_loss_prob_max` / `tot_struct_risk_score_max` (CDSS / TSR — `modeling`'s columns) alongside its own FICO series. `_drop_foreign_series` now filters series whose source table another specialist owns, but ONLY when that owner also ran the turn (otherwise the peek is the figure's only source). `data_query.md` C.2 rule 4b tells specialists to anchor cross-peeks with a point value, never a trend.

**When changing chart SSE payloads:** the frontend lives in `My Drive/Projs/CaseReviewChat` (`PlotPanel.tsx`). Every `chart_pending` needs a terminal event — `chart` or `chart_cancelled`. Adding a third pending-emit site without wiring its terminal path reintroduces this exact bug.
