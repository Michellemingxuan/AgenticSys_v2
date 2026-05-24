# Per-Node Context / Token / Time Instrumentation

**Status**: draft, awaiting user review
**Date**: 2026-05-21
**Tag baseline**: `v0.1.1` (commit `d7a7baf`)

## Problem

Existing telemetry (`ProcessTimer` → `process_phase_timing` / `process_timing_summary` JSONL events; `tools/timing_report.py` reader) captures **duration** at the phase level and aggregates **across the whole log**. It cannot answer:

1. "For chat X, turn Y, what was the prompt the orchestrator's team-construction LLM call actually saw?"
2. "How many input tokens did `specialist.spend_payments` accumulate by round 5? Where is context bloating?"
3. "Show me every node in this turn's reasoning trace, in timestamp order, with input/output excerpt + token counts."

Without those answers, memory- and context-management decisions (compaction policy, KB digest size, prior-history retention) are tuned by feel rather than measurement.

## Goal

Add a structured, queryable record of every LLM call boundary in the system, keyed by `(chat_id, turn_id, node, started_at)`, with the prompt/completion excerpt + token counts + duration captured on the same row. Make it cheap to query "what did this node see, at this time, in this chat?"

Non-goals:
- Replacing `ProcessTimer` or `tools/timing_report.py` (kept as-is — they answer different questions).
- Full prompt archival for compliance/audit (excerpts only by default; full-text behind an env flag).
- Online dashboards (CLI report is sufficient; DB is openable by any SQLite browser / pandas).

## Storage

Single SQLite file at `logs/node_traces.db`, global across sessions so cross-chat comparisons are a SQL query away.

Schema is **Langfuse-parity**: every column Langfuse's `observations` table exposes (input/output/tokens/cost/latency/cached-tokens/tags) has a counterpart here, so the data can drive the same optimization questions a Langfuse UI would answer.

```sql
CREATE TABLE node_trace (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id            TEXT NOT NULL,    -- = session_id, e.g. "case-366132845011-cd558f"
  case_id            TEXT NOT NULL,
  turn_id            TEXT NOT NULL,
  node               TEXT NOT NULL,    -- "specialist.spend_payments.round_2"
  parent_id          INTEGER,          -- FK self-ref; NULL for top-level nodes
  depth              INTEGER NOT NULL, -- 0 = logical node, 1 = LLM round
  started_at         TEXT NOT NULL,    -- ISO-8601 UTC with microseconds
  ended_at           TEXT,
  duration_ms        INTEGER,          -- wall-clock total for the node

  -- Latency breakdown (for "where did the time go?" analysis)
  queue_wait_ms      INTEGER,          -- semaphore wait (firewall_stack.gate)
  llm_call_ms        INTEGER,          -- HTTP request → final response (excl. queue wait)
  ttft_ms            INTEGER,          -- time to first token (streamed calls only)
  overhead_ms        INTEGER,          -- duration_ms - queue_wait_ms - llm_call_ms (parsing, redaction, tool exec)

  -- Model + token accounting (for "how expensive was this?" analysis)
  model                TEXT,
  prompt_chars         INTEGER,
  prompt_tokens        INTEGER,        -- actual when OpenAI usage present, tiktoken-estimated otherwise
  cached_input_tokens  INTEGER,        -- OpenAI prompt-cache hits (usage.prompt_tokens_details.cached_tokens)
  system_prompt_chars  INTEGER,        -- system-prompt portion of prompt_chars (overhead vs. content)
  completion_chars     INTEGER,
  completion_tokens    INTEGER,
  reasoning_tokens     INTEGER,        -- o1/o3 reasoning tokens (usage.completion_tokens_details.reasoning_tokens)
  total_tokens         INTEGER,
  cost_usd             REAL,           -- model × tokens via the price table in tools/node_trace/pricing.py

  -- Input/output (for "what did this node actually see/say?" analysis)
  prompt_excerpt     TEXT,             -- head 400 + " … <N chars elided> … " + tail 200, sanitized
  completion_excerpt TEXT,
  messages_json      TEXT,             -- full structured input (gated by NODE_TRACE_STORE_FULL_IO=1; off by default for size)
  output_json        TEXT,             -- full structured output (same gate)

  -- Categorization + outcomes
  outcome     TEXT,                    -- "ok" | "failed" | "timeout" | "cached" | "dedup_hit"
  error_type  TEXT,
  tags        TEXT,                    -- JSON array: ["cache_hit","retry","kb_digest_present","streaming",…]
  extra_json  TEXT                     -- ad-hoc per-node: max_turns, cache_hit_kind, sub_question_chars, n_kps_in_digest, …
);

CREATE INDEX idx_chat_turn ON node_trace(chat_id, turn_id, started_at);
CREATE INDEX idx_node      ON node_trace(node);
CREATE INDEX idx_started   ON node_trace(started_at);

-- Derived views for the optimization workflows (queries built on these
-- replace what would be Langfuse dashboard widgets).
CREATE VIEW turn_summary AS
  SELECT
    chat_id, case_id, turn_id,
    MIN(started_at) AS turn_started_at,
    MAX(ended_at)   AS turn_ended_at,
    SUM(prompt_tokens)      AS total_prompt_tokens,
    SUM(completion_tokens)  AS total_completion_tokens,
    SUM(cached_input_tokens) AS total_cached_tokens,
    SUM(reasoning_tokens)   AS total_reasoning_tokens,
    SUM(cost_usd)           AS total_cost_usd,
    COUNT(*)                AS n_nodes,
    SUM(CASE WHEN depth = 1 THEN 1 ELSE 0 END) AS n_llm_rounds,
    SUM(CASE WHEN outcome IN ('failed','timeout') THEN 1 ELSE 0 END) AS n_failures
  FROM node_trace
  GROUP BY chat_id, case_id, turn_id;

CREATE VIEW session_summary AS
  SELECT
    chat_id, case_id,
    COUNT(DISTINCT turn_id) AS n_turns,
    SUM(prompt_tokens + completion_tokens) AS total_tokens,
    SUM(cost_usd) AS total_cost_usd,
    MIN(started_at) AS session_started_at,
    MAX(ended_at)   AS session_ended_at
  FROM node_trace
  GROUP BY chat_id, case_id;
```

Connection mode:
- One shared `sqlite3.Connection` per process, opened with `isolation_level=None` (autocommit) and `journal_mode=WAL` so reads (e.g. running `tools/node_trace/turn_report.py` from a second shell during a live turn) don't block writes.
- Writes are serialized through a `threading.Lock`. Concurrency demand is low — at most a few inserts per second even under peak — so contention is not a concern.
- All DB writes are wrapped in `try/except` and log to `EventLogger` on failure. **A logging failure must never break an LLM call or a turn.**

## Repository layout

All trace runtime lives as a sub-package under the existing `tools/` folder:

```
tools/node_trace/
├── __init__.py                # public API re-exports
├── core.py                    # NodeTraceStore + NodeTrace + ACTIVE_NODE + TURN_SCOPE + attach_*
├── pricing.py                 # compute_cost() + per-model price table
├── turn_report.py             # CLI tree (python -m tools.node_trace.turn_report)
├── optimization_report.py     # CLI analytics (python -m tools.node_trace.optimization_report)
└── _io.py                     # shared SQLite read helpers
```

Tests mirror this layout under the repo's centralized test tree:
`tests/test_tools/test_node_trace/test_core.py`, `test_pricing.py`,
`test_firewall_hook.py`, `test_safechain_hook.py`, `test_turn_report.py`,
`test_optimization_report.py`.

Files OUTSIDE `tools/node_trace/` only change to call into the package's
public API: `llm/firewall_client.py`, `llm/safechain_client.py`,
`llm/firewall_stack.py`, `agent_factories/chat_agent.py`,
`agent_factories/redacting_tool.py`, `server.py`. The split is clean: the
trace package owns the data model + storage + readers; the consumers own
the LLM transport + agent wiring.

## Components

### 1. `tools/node_trace/core.py`

Two classes:

- **`NodeTraceStore`** — owns the SQLite connection + lock. Constructed once at server startup (or test setup) and passed into agents/sessions. Exposes `insert(row: dict) -> int` returning the new row's `id`, and `update(id: int, **fields)` for finalizing duration/output after the call returns.

- **`NodeTrace`** — async context manager. Usage:
  ```python
  async with NodeTrace(store, chat_id=..., turn_id=..., node="specialist.spend_payments", depth=1) as nt:
      result = await Runner.run(...)
      nt.attach_completion(result.final_output_text, completion_tokens=...)
  ```
  On `__aenter__`: INSERT a row with `started_at`, `node`, `parent_id` (resolved from a `contextvars.ContextVar[NodeTrace | None]`), `depth`. Push self onto the contextvar. On `__aexit__`: UPDATE the same row with `ended_at`, `duration_ms`, `outcome`, completion fields, and pop the contextvar.

  Parent-id resolution via contextvar means nested `NodeTrace` blocks (specialist Runner.run → its internal LLM rounds) form the tree automatically without callers having to pass parent_id explicitly.

### 2. Token + latency + cost capture hooks in LLM clients

- **`llm/firewall_client.py`** (`_FirewalledChatCompletions.create`): after the OpenAI call returns, if `response.usage` is set, attach to the active `NodeTrace`:
  - `prompt_tokens`, `completion_tokens`, `total_tokens` (top-level)
  - `cached_input_tokens` from `usage.prompt_tokens_details.cached_tokens` (set when OpenAI prompt-caching kicks in — late 2024+ models)
  - `reasoning_tokens` from `usage.completion_tokens_details.reasoning_tokens` (o1/o3 family)
  - `cost_usd` computed via `node_trace.pricing.compute_cost()` (a small per-model `$/1M tokens` table; safe default 0 for unknown models)
- **`llm/safechain_client.py`** (`_SafeChainChatCompletions._invoke`): safechain returns no usage object. Use `tiktoken.encoding_for_model(model)` (with fallback to `cl100k_base`) to estimate tokens from `combined` (input) and `text` (output). No cached/reasoning fields. `cost_usd` is computed the same way but marked in `extra_json.cost_basis = "estimated"`.
- **`llm/firewall_stack.py`** (`FirewallStack.gate`): the existing `waited_ms` measurement (already computed for `firewall_semaphore_wait`) is also attached to the active `NodeTrace` as `queue_wait_ms`.
- **Latency split**: each LLM client records `llm_call_ms` (HTTP request → response, excl. queue wait); `overhead_ms` is computed at `NodeTrace.__aexit__` as `duration_ms - queue_wait_ms - llm_call_ms`.
- **TTFT**: only the streamed orchestrator path can capture this — `Runner.run_streamed` exposes the first stream event. Server.py records `ttft_ms` on the active `orchestrator` node when the first `run_item_stream_event` lands. Non-streaming calls leave this NULL.
- **System-prompt accounting**: when the messages list contains a `system` role, its content length is stored as `system_prompt_chars` (so optimization queries can split overhead from content). Useful for spotting prompt-bloat regressions.
- **Excerpt capture**: head 400 + tail 200 of the flattened prompt + completion, both sanitized via `llm.firewall_stack.sanitize_message`.
- **Full I/O (opt-in)**: when `NODE_TRACE_STORE_FULL_IO=1` is set, `messages_json` and `output_json` columns are filled with the structured JSON payload. Off by default — bloats the DB ~10×. Turn on per debugging session.
- **Tags**: each node may carry tags like `cache_hit`, `dedup_hit`, `retry`, `streaming`, `kb_digest_present`, `firewall_rejected_once`, `near_duplicate`. Pushed onto the active `NodeTrace.tags` list via `attach_tag(...)`. Persisted as a JSON array.

Env overrides:
- `NODE_TRACE_EXCERPT_HEAD` (default 400)
- `NODE_TRACE_EXCERPT_TAIL` (default 200)
- `NODE_TRACE_FULL_PROMPT=1` — skip truncation entirely (debug only; will bloat the DB)
- `NODE_TRACE_DISABLE=1` — disable the entire layer (escape hatch for emergencies)

### 3. Wire-up sites

| Site | Node name(s) | Depth |
|---|---|---|
| `agent_factories/chat_agent.py` :: `screen` | `chat.screen` | 0 |
| `agent_factories/chat_agent.py` :: `relevance_check` (if separate call) | `chat.relevance_check` | 0 |
| `server.py` orchestrator block (~lines 1107-1130) | `orchestrator` | 0 |
| Inside orchestrator: first LLM call | `orchestrator.team_construction` | 1 |
| Inside orchestrator: synthesis LLM call | `orchestrator.synthesis` | 1 |
| `agent_factories/redacting_tool.py` :: specialist `Runner.run` | `specialist.<name>` | 1 |
| Inside specialist: each LLM round | `specialist.<name>.round_<N>` | 2 |
| `agent_factories/redacting_tool.py` :: distiller `Runner.run` | `distiller.<name>` | 1 |

Round-level (depth 2) capture leverages the SDK's run hooks; no manual round-counting in the wire-up call sites.

### 4. `tools/node_trace/turn_report.py`

CLI for reading a single turn's tree out of the DB.

```
python -m tools.node_trace.turn_report --chat case-…-cd558f --turn 59b7e9859397
python -m tools.node_trace.turn_report --last
python -m tools.node_trace.turn_report --last --json
python -m tools.node_trace.turn_report --last --full-excerpts
python -m tools.node_trace.turn_report --last --rounds         # expand depth-1 LLM rounds
python -m tools.node_trace.turn_report --last --cost           # include $ column
```

Default text output (collapsed to depth 0):
```
chat case-…-cd558f  turn 59b7e9859397  total=251.4s  in=412k tok (cached=18k)  out=8.2k tok  $0.094
├─ chat.redact                            0.4s    in=412      out=18     $0.0001    "Was the payment…"
├─ chat.relevance_check                   0.8s    in=512      out=24     $0.0002    "Reviewer question…"
├─ orchestrator                         195.4s    in=66k      out=3.8k   $0.0481    [streaming, ttft=1.2s]
├─ specialist.spend_payments             98.2s    in=148k     out=2.1k   $0.0218    [kb_digest_present]
├─ specialist.modeling                  187.6s    in=201k     out=3.4k   $0.0294
└─ distiller.spend_payments               7.5s    in=8.9k     out=1.1k   $0.0019
```

With `--rounds`, specialist branches expand into per-round children showing **token growth per round** (the main signal for context-bloat analysis).

### 5. `tools/node_trace/optimization_report.py`

Higher-level analytical CLI surfacing the three optimization questions explicitly:

```
python -m tools.node_trace.optimization_report memory       # context-growth-per-round, KB-digest impact, input-history accumulation
python -m tools.node_trace.optimization_report tokens       # cache-hit rate per node, system-prompt overhead, top spenders
python -m tools.node_trace.optimization_report latency      # queue wait %, TTFT distribution, slowest nodes, parallel underutilization
python -m tools.node_trace.optimization_report --turn <id>  # all three sections scoped to one turn
```

Concrete signals each section computes (each is one `SELECT` against the `node_trace` table + the views):

| Section | Signal | Why it matters |
|---|---|---|
| memory | per-specialist `prompt_tokens` curve across round_1..round_N | identifies which specialist's context bloats fastest; flags compaction candidates |
| memory | KB-digest size by turn (extra_json.n_kps_in_digest) | shows whether warmth-digest is exceeding budget |
| memory | input_history_chars by turn-start node | turn-over-turn cross-turn memory growth |
| tokens | cached_input_tokens / prompt_tokens ratio per node | OpenAI prompt-cache hit rate; flags nodes where reordering messages could lift cache |
| tokens | system_prompt_chars / prompt_chars ratio per node type | system-prompt overhead; flags places to trim instructions |
| tokens | top-10 nodes by total_tokens (lifetime) | where the token budget actually goes |
| latency | queue_wait_ms / duration_ms ratio per node | semaphore contention proxy; flags concurrency caps as binding |
| latency | TTFT distribution for orchestrator nodes | user-perceived "first paint" latency |
| latency | overhead_ms / duration_ms per node | non-LLM time (parsing, redaction, tool exec) eating budget |
| latency | parallel underutilization: when N specialists run, max(duration) vs. sum(duration) | how much of the parallelism is real vs. serialized behind locks |

Both readers share helpers in `tools/node_trace/_io.py` (DB connection + query builders) so the reporting code stays DRY.

Plain SQL stays available for ad-hoc work — the DB has no proprietary format.

## Data flow

```
[Runner.run / chat call site]
        │
        ▼
[NodeTrace.__aenter__]  ──INSERT──►  node_traces.db (row id = parent for nested calls)
        │
        ▼
[LLM client .create]
   ├─ firewall_client → response.usage    ─attach_usage()─►  active NodeTrace
   └─ safechain_client → tiktoken estimate ─attach_usage()─►  active NodeTrace
        │
        ▼
[NodeTrace.__aexit__]   ──UPDATE──►  node_traces.db (fill ended_at, duration, outcome, excerpts)
```

## Error handling

- Any DB exception (lock contention timeout, schema mismatch, disk full) is caught at the `NodeTrace` boundary, logged once per process to `EventLogger` as `node_trace_db_error`, and swallowed. The wrapped LLM call proceeds.
- If `tiktoken` import fails, the safechain path falls back to `chars // 4` heuristic and tags `extra_json.token_estimate_method = "chars_div_4"`.
- If the contextvar resolution returns no active NodeTrace (e.g. a tool call from a code path that wasn't wired up), `attach_usage` is a no-op.

## Testing

- `tests/test_tools/test_node_trace/test_core.py`:
  - `NodeTraceStore` insert/update round-trip on a temp SQLite file
  - Nested `NodeTrace` blocks produce correct `parent_id` chains
  - DB failures are swallowed without raising
- `tests/test_tools/test_node_trace/test_turn_report.py`:
  - Tree-print output for a synthetic 1-turn fixture
  - `--json` matches expected schema
- Integration: extend an existing server test to assert that running one turn populates at least one `chat.screen` + one `orchestrator.*` row in a temp DB.

## Migration / rollout

- Pure-additive: existing `ProcessTimer` events untouched, existing readers continue working.
- DB file is created lazily on first write. No migration script needed for fresh installs.
- For an existing repo: drop the `.db` file to reset.
- Disabling: set `NODE_TRACE_DISABLE=1` — all `NodeTrace` blocks become no-ops, no DB file touched.

## Optimization workflows this enables

Each scenario below is one query against the schema — concrete usage of the data.

**1. "Why is this turn slow?"**
- `SELECT node, duration_ms, queue_wait_ms, llm_call_ms, overhead_ms FROM node_trace WHERE turn_id = ? ORDER BY duration_ms DESC LIMIT 10`
- Reveals whether the bottleneck is LLM wall-clock (model-side), queue wait (cap too tight), or overhead (parsing/redaction).

**2. "Which specialist's context is bloating?"**
- `SELECT node, prompt_tokens FROM node_trace WHERE node LIKE 'specialist.%.round_%' AND chat_id = ? ORDER BY node, started_at`
- A monotonically rising prompt_tokens curve across `round_1`..`round_N` means context isn't being compacted between rounds → compaction policy needs tightening.

**3. "Are we benefiting from prompt caching?"**
- `SELECT node, AVG(cached_input_tokens * 1.0 / NULLIF(prompt_tokens, 0)) FROM node_trace WHERE cached_input_tokens IS NOT NULL GROUP BY node`
- Low ratio means we're paying for fresh tokens we could be caching → reorder messages so the cacheable prefix is stable.

**4. "Where does the system prompt eat the budget?"**
- `SELECT node, AVG(system_prompt_chars * 100.0 / NULLIF(prompt_chars, 0)) FROM node_trace GROUP BY node`
- High % on a low-content call means the system prompt is over-engineered for that node.

**5. "Are specialists actually running in parallel?"**
- Within one turn: `SUM(duration_ms)` over depth-0 specialist nodes vs. `MAX(ended_at) - MIN(started_at)` over the same set. If sum ≫ max, parallelism is being lost (likely to the semaphore).

**6. "What is one turn costing in dollars?"**
- `SELECT * FROM turn_summary WHERE turn_id = ?` — total_cost_usd, total_tokens, total_cached_tokens in one row.

These are the queries `tools/node_trace/optimization_report.py` ships with; new ones can be added without schema changes thanks to the `extra_json` + `tags` flexibility.

## Open questions (none blocking; capture for later)

- Future: a periodic `tools/compact_traces.py` to age out / archive rows older than N days, if the DB grows beyond comfortable size.
- Future: if cost tracking grows beyond a flat per-model table (tiered pricing, finetune surcharges), move the price table out of `tools/node_trace/pricing.py` and into a YAML config under `config/`.
- Future: optional Langfuse-self-hosted exporter — `tools/node_trace/export_langfuse.py` reads the SQLite store and pushes rows as Langfuse observations, if/when the team wants the web UI without changing the runtime path.
