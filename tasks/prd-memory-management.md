# PRD: Memory Management — KB, Visualization, Fast-Path Routing

## Introduction / Overview

The case-review agent system slows down across long reviewer conversations because the orchestrator's `input_history` accumulates every turn's full specialist tool outputs (5–50 KB each), feeding 100–500 KB of context back to gpt-4.1 by turn 8–10. Two related deficiencies compound this:

1. Specialists have only intra-orchestrator-run memory, so they can't reuse prior findings across turns and re-run identical `summarize_trend` / `aggregate_column` queries on every follow-up.
2. Quantitative findings ship as text + numbers in narrative answers; reviewers find numeric trends and concentrations easier to interpret as charts than as prose.

This feature introduces a **session-scoped, per-specialist Knowledge Base (KB)** populated by a second-pass distiller agent, with **inline chart rendering** of quantitative claims and a **soft-signal fast-path** that lets the orchestrator reuse warm specialists on follow-ups.

Phase 1 (KB infrastructure + distiller + history pruning) shipped in commits `24096cc..d985ffb` (tag `v0.1.0`). This PRD covers Phase 2 (visualization) and Phase 3 (fast-path routing).

## Goals

- **Reduce per-turn latency on long sessions** — orchestrator's input_history stops growing unbounded; specialists answer follow-ups from KB instead of re-querying.
- **Improve interpretability** — every KnowledgePoint with a `viz` spec auto-renders to a chart embedded in the agent's answer, alongside the prose narrative.
- **Preserve answer quality and audit** — older KB entries are never deleted (audit trail); supersession is implicit (latest-per-topic wins for retrieval). Charts are reproducible from the stored numbers.
- **Keep orchestrator decisions LLM-driven** — the fast-path is a *signal* the orchestrator considers, not a hard rule that bypasses team construction.

## User Stories

### US-001: Render KP charts to PNG at distillation time *(Phase 2)*

**Description:** As a reviewer, I want quantitative findings (monthly trends, merchant concentrations, threshold breaches) shown as charts inline in the agent's answer, so I can see the shape of the data at a glance instead of parsing prose and number lists.

**Acceptance Criteria:**
- [ ] New module `tools/viz_renderer.py` exposes `render_chart(kp: dict, out_dir: Path) -> str | None` returning a relative URL path (or None on failure).
- [ ] Supports three viz kinds matching `KnowledgePoint.viz.kind`: `trend` → matplotlib line, `bar` → bar chart, `share` → horizontal bar (stable for many groups; pie reserved for ≤ 4 slices).
- [ ] Reads the `numbers` array from the KP, mapping `viz.x_field` / `viz.y_field` (or `group` / `value` for breakdowns) to the chart axes.
- [ ] PNG output goes to `reports/<case_id>/charts/<turn_id>-<kp_topic>.png` — filename uses the topic slug for human readability + the turn id for chronological sort.
- [ ] Failures (matplotlib error, missing fields, malformed numbers) are logged but never raised; the function returns None and the caller continues.
- [ ] Typecheck + tests pass.

### US-002: Persist Vega-Lite spec alongside the PNG *(Phase 2)*

**Description:** As a downstream consumer (export tooling, future interactive frontend, audit reports), I want the chart spec stored as Vega-Lite JSON in the KB so I can re-render the same data interactively or in a different format without re-running the original tool call.

**Acceptance Criteria:**
- [ ] `tools/viz_renderer.py` exposes `kp_to_vega_spec(kp: dict) -> dict | None` that returns a minimal Vega-Lite v5 spec (line / bar mark, the `numbers` inlined as `data.values`, encodings derived from `viz.kind`/`x_field`/`y_field`).
- [ ] After distillation, when a KP gets a chart rendered, the KB entry gains both `image_path: str` and `vega_spec: dict` fields.
- [ ] Tests: spec roundtrips through `json.dumps` / `json.loads` cleanly; `numbers` are preserved verbatim in `data.values`.
- [ ] Typecheck + tests pass.

### US-003: Append rendered charts to agent_message *(Phase 2)*

**Description:** As a reviewer, when the orchestrator's answer cites quantitative findings that were captured as KPs this turn, the corresponding charts appear at the bottom of the chat message under a `**Supporting charts**` heading.

**Acceptance Criteria:**
- [ ] After the orchestrator emits a FinalAnswer, server.py scans `sess.specialist_kb[*]` for KPs whose `captured_at_turn == turn_id` AND have an `image_path`.
- [ ] Builds a `**Supporting charts**\n` section appending `![{topic}]({url})` lines, one per chart, in distillation order.
- [ ] If no charts were created this turn, no section is added (no empty header).
- [ ] The chart section is appended to `answer_text` BEFORE the `final` and `agent_message` SSE events are emitted, so frontend renders in one pass.
- [ ] Cached-replay path (`qa_cache` hit) does not duplicate chart appending — replayed answers already include the charts from their original turn.
- [ ] Typecheck + tests pass.

### US-004: Serve chart files via Flask route *(Phase 2)*

**Description:** As the frontend, when I encounter `![chart](/api/cases/<id>/charts/<file>)` in the agent_message markdown, I need that URL to resolve to the actual PNG.

**Acceptance Criteria:**
- [ ] New Flask route: `GET /api/cases/<case_id>/charts/<filename>` serves files from `reports/<case_id>/charts/` if the path resolves inside that directory.
- [ ] Path-traversal guard: reject any filename containing `..`, leading `/`, or backslashes; return 404.
- [ ] Returns 404 when the file doesn't exist; returns 200 + `image/png` content type when it does.
- [ ] CORS allowance matches the existing `/api/cases/*` pattern (origins `localhost:5173`, `127.0.0.1:5173`).
- [ ] Tests: traversal attack returns 404; valid file returns content; missing file returns 404.

### US-005: Orchestrator KB-warmth signal in question prefix *(Phase 3)*

**Description:** As the orchestrator, when I'm choosing which specialists to call on a follow-up, I want to know which specialists are already "warm" (have non-empty KBs from earlier turns) so I can favor reusing them instead of building a fresh team.

**Acceptance Criteria:**
- [ ] Server.py builds a one-line warmth hint before each turn: `[KB-warmth: spend_payments (3 KPs), modeling (5 KPs), bureau (0 KPs). Strongly consider reusing warm specialists for in-domain follow-ups.]`.
- [ ] Specialists with zero KPs are omitted from the hint (no "(0 KPs)" noise — they're the same as never-called).
- [ ] Hint is prepended to the redacted user question, not injected as a separate role message (keeps SDK input shape simple).
- [ ] When `sess.specialist_kb` is empty (first turn), no hint is prepended.
- [ ] After this change, the existing `team_construction.md` "Follow-ups & near-duplicates — REUSE THE PRIOR TEAM" rule has a runtime signal to anchor on, not just conversation context.
- [ ] Typecheck + tests pass.

### US-006: Update team_construction skill to leverage the warmth signal *(Phase 3)*

**Description:** As the orchestrator's prompt author, I want the team_construction skill to explicitly tell the LLM to read the KB-warmth hint when present and treat it as a routing input.

**Acceptance Criteria:**
- [ ] `skills/workflow/team_construction.md` adds a short paragraph under "Selection rules": "When the user message begins with `[KB-warmth: …]`, treat that as the primary signal for follow-up routing — favor reusing warm specialists unless the question's domain has clearly shifted."
- [ ] The existing rule about reusing the prior team is preserved; the warmth hint augments it.
- [ ] No hard skip of team construction — orchestrator retains LLM judgment.

### US-007: KB-warmth event logging *(Phase 3, observability)*

**Description:** As an operator debugging a slow / unexpectedly-routed turn, I want the warmth hint and the resulting team in the EventLogger so I can trace the routing decision.

**Acceptance Criteria:**
- [ ] Server logs a `kb_warmth_hint_emitted` event per turn (when non-empty) with: `turn_id`, `warm_specialists` (list of `{name, n_kps}`), `hint_length`.
- [ ] No PII, no question text in the event payload — just structural counts.

## Functional Requirements

- FR-1: The system must render any KnowledgePoint with `viz` non-null and `numbers` length ≥ 1 to both a PNG (under the case folder) and a Vega-Lite JSON spec (stored on the KP).
- FR-2: When a turn produces ≥ 1 chart, the agent's `answer_text` must include a `**Supporting charts**` section listing each rendered chart as a markdown image. Empty otherwise.
- FR-3: The Flask app must serve PNGs from `reports/<case_id>/charts/` via `GET /api/cases/<case_id>/charts/<filename>`, rejecting path-traversal attempts.
- FR-4: Before each orchestrator run with a non-empty session KB, the server must prepend a one-line `[KB-warmth: …]` hint to the user question, summarizing which specialists carry KPs and how many.
- FR-5: The team_construction skill must instruct the orchestrator to treat the warmth hint as a primary routing signal for follow-ups.
- FR-6: All chart-rendering and KB-warmth operations must degrade gracefully on failure (logged, no exception propagated to the user-facing turn flow).

## Non-Goals (Out of Scope)

- **Interactive client-side charts.** Phase 2 ships static PNG + Vega-Lite spec; the spec is preserved for future use but no interactive UI work is in scope here.
- **Hard-skip orchestrator routing.** Phase 3 deliberately leaves team construction as an LLM decision; programmatically bypassing the team_construction prompt is explicitly NOT done in this PRD (would be a separate "exact-near-duplicate fast-path" feature, possibly later).
- **RAG over KB.** The KB digest is passed verbatim to the specialist on each call. Embedding-based retrieval is deferred until KBs grow past ~50 entries per specialist (not observed in current sessions).
- **Cross-specialist KB sharing for non-`general_specialist` domains.** Domain specialists see only their OWN KB digest; only `general_specialist` (when wired in a future iteration) would read across domains. Not in scope here.
- **Chart-rendering for non-quantitative claims.** KPs without a `viz` spec or with empty `numbers` are NOT rendered.
- **Re-rendering historical KPs.** Only KPs `captured_at_turn == current_turn_id` get rendered when produced; older KPs that were generated before Phase 2 shipped don't get back-filled with charts.

## Design Considerations

- **PNG filename pattern**: `<turn_id>-<topic_slug>.png`. Turn id keeps charts chronological per case folder; topic slug makes manual review human-friendly.
- **`reports/<case_id>/charts/` is created lazily** by the renderer (`Path.mkdir(parents=True, exist_ok=True)`) — no bootstrap step needed.
- **Markdown image embedding** in agent_message uses absolute API path (`/api/cases/<case_id>/charts/<file>`) so the React app's existing markdown renderer resolves it correctly.
- **Vega-Lite spec shape**: minimal v5 (`{"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "data": {"values": [...]}, "mark": "line", "encoding": {...}}`). No advanced features (no layers, no transforms) — keeps the spec interpretable and lightweight in JSONL logs.
- **Renderer module `tools/viz_renderer.py`** isolated so it can be unit-tested without spinning up the agent graph.
- **KB-warmth hint placement**: as a prefix to the user's question text, not a separate `system` role message. Keeps the SDK input shape unchanged (a single string or a `[history…, {"role":"user", "content":"<hint>\n\n<question>"}]` list).

## Technical Considerations

- **Matplotlib**: requires the `matplotlib` package to be installed. Must add to `requirements.txt`. Use the `Agg` backend (non-interactive, no display required) so it works on a headless server.
- **Server-side state**: chart files persist on disk under `reports/<case_id>/charts/`. The existing `/rewind` endpoint should NOT delete chart files (audit trail), but a future "reset case" admin path might.
- **Cache replay**: when `qa_cache` returns a cached answer, the cached `answer_text` already contains the chart markdown from the original turn — replaying preserves the chart references. Confirmed by inspection of server.py:359-373.
- **Failure isolation**: chart rendering happens inside `_distill_and_persist` which already wraps everything in try/except; renderer failures stay silent in user-facing flow but log via the EventLogger.

## Success Metrics

- **Chart coverage**: ≥ 70% of KPs with `viz` non-null AND `numbers` length ≥ 2 produce a non-None image_path on first session of a representative case.
- **Latency neutrality**: per-turn wall-clock latency increases by < 1.5s on a turn that creates 5 charts (matplotlib render is the only added cost; runs after the user-facing answer has streamed).
- **Reuse rate on follow-ups**: when a follow-up is in the same domain as the previous turn AND the prior team's KBs are non-empty, the orchestrator chooses the same team in ≥ 80% of cases (manual review of 10 sessions).
- **No regressions**: 249/249 existing tests + new tests pass; no new error events appear in case logs after rollout.

## Open Questions

- Should the **orchestrator be told** when a KP has a chart, so it can refer to the chart by topic in its prose ("see the monthly_spend_trend chart below")? Currently the answer is built without that awareness — charts just appear underneath. If reviewers find that disconnected, a small skill update could add chart-aware narration.
- Once `general_specialist` runs are observed in production, should it see all specialists' KBs (full read access) or just a digest summary (one line per domain)? Out of scope for this PRD but worth tracking.
- Vega-Lite specs in JSONL audit logs may bloat them — acceptable for now since most KPs don't have charts. Revisit if the logs become unwieldy.
