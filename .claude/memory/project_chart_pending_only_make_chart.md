---
name: project-chart-pending-only-make-chart
description: The "Working on the plot…" placeholder (chart_pending SSE) fires ONLY for the explicit make_chart tool; the auto-chart path that's actually used in practice emits no chart_pending, so the placeholder is usually dormant.
metadata:
  type: project
---

There are **two** chart-producing paths in AgenticSys_v2, and only one of them emits the `chart_pending` SSE event that drives the frontend "Working on the plot…" placeholder (PlotPanel.tsx in `My Drive/Projs/CaseReviewChat`):

1. **`make_chart` tool** (`tools/data_viz_tools.py:212`) — the specialist explicitly calls it. Emits `chart_pending` (keyed by `(specialist, topic, kind)`) BEFORE rendering. This is the ONLY `chart_pending` emit site in the repo.
2. **Auto-chart** (`_auto_chart_from_tool_outputs` in `agent_factories/redacting_tool.py:489`) — automatic, data-driven from `summarize_trend`/`batch_summarize_trend`/`summarize_by_group` tool outputs, no LLM call. Logs `auto_chart_rendered`; the low-level renderer (`tools/viz_renderer.py:render_chart`) logs `viz_rendered`. It does **NOT** emit `chart_pending`.

**In practice specialists usually DON'T call `make_chart`** — charts come from the auto-chart path. Tell-tale: the JSONL run log shows `viz_rendered` + `auto_chart_rendered` and **no** `make_chart`. So the placeholder is effectively dormant: the user gets the text answer, then the chart pops into the Plot panel via the end-of-turn `chart` event (emitted in `server.py` ~`_collect_turn_charts`, after the distiller drain) with no preceding loading cue (~5–15s silent gap).

**This is backend-agnostic** — identical in dev (openai) and prod (safechain). The emit path (`emit_event` closure → `sess.emit`, `server.py:1107`) and tool/agent wiring are shared; only the LLM HTTP transport differs ([[safechain_dual_environment]]). So the placeholder's absence is about *which chart path runs*, not which backend.

**Timing note for any fix:** the auto-chart task is `asyncio.create_task`'d the moment the specialist's `redacting_tool` returns (`redacting_tool.py:1500-1518`) and appended to `_pending_distillers`; it runs concurrently during orchestrator synthesis and is merely *awaited* at end-of-turn — it does NOT wait until end-of-turn to start. So emitting `chart_pending` right before each `render_chart` inside `_render_auto_charts` already fires early (precise `(specialist, topic)` keys that the existing frontend clears via `upsertChart` — no frontend change, no false positives). A coarse "charts pending" at task-schedule time fires only marginally earlier but doesn't know topics yet → needs a sentinel key + a new clear-on-turn_done path, and dangles if parsing yields no charts. See [[feedback_wire_frontend_with_backend]] when changing SSE/chart payloads.
