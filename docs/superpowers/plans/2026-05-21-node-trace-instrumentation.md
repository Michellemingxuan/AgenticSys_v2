# Per-Node Context / Token / Time Instrumentation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Project commit rule:** This repo's `.claude/memory/feedback_commit_only_when_asked.md` says NEVER auto-commit or auto-push. Each task lists a `git commit` step at the natural TDD cadence, but the executor MUST hold the commit and offer it to the user at the end of the task instead of running it. Tests + green-bar are the per-task acceptance gate.

**Goal:** Add a **Langfuse-parity** SQLite-backed record of every LLM call boundary in the system, capturing not just timing + tokens but the full optimization-relevant data: cached-input tokens (prompt-cache hits), reasoning tokens (o1/o3), `cost_usd`, queue-wait split from LLM-call split from overhead, TTFT for streamed calls, system-prompt size, optional full structured I/O, and free-form tags. Keyed by `(chat_id, turn_id, node, started_at)`.

**Architecture:** A new `NodeTraceStore` (SQLite, single global file `logs/node_traces.db`, WAL mode, plus `turn_summary` + `session_summary` views) + `NodeTrace` async context manager (`tools/node_trace/core.py`) wraps every logical node (`chat.*`, `orchestrator`, `specialist.*`, `distiller.*`). The two LLM clients (`firewall_client.py`, `safechain_client.py`) intercept `.create()` and, when an active parent `NodeTrace` exists in the contextvar, auto-create a depth-1 `round_<N>` child row. `firewall_stack.gate()` writes `queue_wait_ms` onto the active node. The orchestrator's streamed run records `ttft_ms` when the first stream event lands. A `tools/node_trace/pricing.py` table converts tokens → `cost_usd`. Two readers — `tools/node_trace/turn_report.py` (per-turn tree) and `tools/node_trace/optimization_report.py` (memory / tokens / latency analytical rollups) — read from the same DB.

**Tech Stack:** Python 3.11+, SQLite3 (stdlib), tiktoken, asyncio + contextvars, pytest + pytest-asyncio, rich (already in requirements.txt) for the CLI tree print.

---

## File Structure

**New sub-package — `tools/node_trace/`** (all trace runtime under the existing `tools/` folder):

```
tools/node_trace/
├── __init__.py                 # public API re-exports: NodeTrace, NodeTraceStore, ACTIVE_NODE, TURN_SCOPE, attach_* helpers
├── core.py                     # NodeTraceStore (DB + schema + views) + NodeTrace (async CM) + ACTIVE_NODE/TURN_SCOPE contextvars + attach_usage/tag/latency/io/extra + _open_node + _NullNode
├── pricing.py                  # _PRICES table + compute_cost()
├── turn_report.py              # CLI tree reader (python -m tools.node_trace.turn_report)
├── optimization_report.py      # CLI memory/tokens/latency analytics (python -m tools.node_trace.optimization_report)
└── _io.py                      # shared open_db + row helpers used by both readers
```

**Tests — `tests/test_tools/test_node_trace/`** (mirrors the package):

```
tests/test_tools/test_node_trace/
├── __init__.py
├── test_core.py                # NodeTraceStore + NodeTrace + attach_* + _open_node
├── test_pricing.py             # compute_cost
├── test_firewall_hook.py       # OpenAI client wire-up (incl. queue_wait_ms via FirewallStack.gate)
├── test_safechain_hook.py      # safechain client wire-up
├── test_turn_report.py         # tree reader
└── test_optimization_report.py # analytical reader
```

**Modified files** (call into the new package; nothing trace-specific lives outside it):

- `requirements.txt` — add `tiktoken>=0.7.0,<1.0.0`
- `llm/firewall_client.py` — wrap `.create()`; capture `response.usage` (incl. cached + reasoning tokens); compute cost; record `llm_call_ms`; auto-create `round_<N>` child rows
- `llm/safechain_client.py` — wrap `.create()`; tiktoken-estimate; compute cost; record `llm_call_ms`; auto-create `round_<N>` child rows
- `llm/firewall_stack.py` — write `waited_ms` onto the active node via `attach_latency(queue_wait_ms=...)`
- `agent_factories/chat_agent.py` — `NodeTrace` blocks around `redact` / `relevance_check` / `clarify_intent`
- `agent_factories/redacting_tool.py` — `NodeTrace` blocks around specialist + distiller `Runner.run`; tag `cache_hit` / `kb_digest_present`; record `n_kps_in_digest` via `attach_extra`
- `server.py` — construct one `NodeTraceStore` at startup; attach to `Session` + `AppContext`; set `TURN_SCOPE` per turn; wrap orchestrator block with `NodeTrace("orchestrator")`; record `ttft_ms` on first stream event

---

### Task 1: Add tiktoken dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add dependency line**

Edit `requirements.txt` and insert `tiktoken>=0.7.0,<1.0.0` after the `openai-agents` line:

```
openai>=1.30.0,<2.0.0
openai-agents>=0.0.10
tiktoken>=0.7.0,<1.0.0
pydantic>=2.0.0,<3.0.0
…
```

- [ ] **Step 2: Verify it's importable in the current env**

Run: `python -c "import tiktoken; print(tiktoken.__version__)"`
Expected: prints a version (e.g. `0.11.0`). No need to `pip install` — tiktoken is already in the dev env (verified at plan-write time).

- [ ] **Step 3: Commit (offer to user; do not auto-run)**

```
git add requirements.txt
git commit -m "deps: pin tiktoken for node-trace token estimation"
```

---

### Task 2: NodeTraceStore — package scaffold + schema + insert/update

**Files:**
- Create: `tools/node_trace/__init__.py` (public-API re-exports — populated incrementally as later tasks add symbols)
- Create: `tools/node_trace/core.py` (NodeTraceStore class only — `NodeTrace` + contextvars come in Task 3)
- Create: `tests/test_tools/node_trace/__init__.py` (empty marker)
- Create: `tests/test_tools/test_node_trace/test_core.py`

- [ ] **Step 1: Create the package directories**

Run: `mkdir -p node_trace tests/test_node_trace && touch tools/node_trace/__init__.py tests/test_tools/node_trace/__init__.py`

Edit `tools/node_trace/__init__.py` to seed the re-export surface:

```python
"""Per-node trace package: SQLite-backed observability for every LLM call boundary.

Public API is re-exported here so call sites can use ``from tools.node_trace import …``
without coupling to internal module layout.
"""
from tools.node_trace.core import NodeTraceStore  # re-exported as Task 3 / 4 add more

__all__ = ["NodeTraceStore"]
```

Later tasks extend `__all__` as `NodeTrace`, `ACTIVE_NODE`, `TURN_SCOPE`, `TurnScope`, `attach_usage`, `attach_tag`, `attach_latency`, `attach_io`, `attach_extra`, `_open_node` land in `core.py`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_tools/test_node_trace/test_core.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from tools.node_trace import NodeTraceStore


def test_store_creates_schema_and_inserts(tmp_path: Path) -> None:
    db_path = tmp_path / "traces.db"
    store = NodeTraceStore(str(db_path))
    row_id = store.insert(
        chat_id="case-X-aaaa",
        case_id="X",
        turn_id="turn1",
        node="chat.redact",
        parent_id=None,
        depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    assert isinstance(row_id, int) and row_id > 0

    # Read back via plain sqlite to confirm schema + row landed.
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "SELECT chat_id, case_id, turn_id, node, depth FROM node_trace WHERE id = ?",
        (row_id,),
    )
    row = cur.fetchone()
    assert row == ("case-X-aaaa", "X", "turn1", "chat.redact", 0)


def test_store_update_finalizes_row(tmp_path: Path) -> None:
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    row_id = store.insert(
        chat_id="c", case_id="c", turn_id="t",
        node="chat.redact", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    store.update(
        row_id,
        ended_at="2026-05-21T00:00:01.500000+00:00",
        duration_ms=1500,
        prompt_tokens=120,
        completion_tokens=8,
        total_tokens=128,
        outcome="ok",
    )
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    row = conn.execute(
        "SELECT duration_ms, prompt_tokens, completion_tokens, total_tokens, outcome "
        "FROM node_trace WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row == (1500, 120, 8, 128, "ok")


def test_store_swallows_db_failure(tmp_path: Path, monkeypatch) -> None:
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    # Corrupt the connection so writes raise.
    store._conn.close()
    # Must not raise; returns -1 / None sentinel.
    assert store.insert(
        chat_id="c", case_id="c", turn_id="t",
        node="x", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    ) == -1
    store.update(1, outcome="ok")  # also must not raise
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_tools/test_node_trace/test_core.py -v`
Expected: FAIL with `ImportError: cannot import name 'NodeTraceStore' from 'node_trace'` (the `__init__.py` re-exports a name that has no source module yet).

- [ ] **Step 4: Implement NodeTraceStore**

Create `tools/node_trace/core.py`:

```python
"""SQLite-backed per-node trace store.

Each row = one LLM-call boundary in the reasoning trace, keyed by
(chat_id, turn_id, node, started_at). See
docs/superpowers/specs/2026-05-21-node-trace-instrumentation-design.md.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_trace (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id              TEXT NOT NULL,
  case_id              TEXT NOT NULL,
  turn_id              TEXT NOT NULL,
  node                 TEXT NOT NULL,
  parent_id            INTEGER,
  depth                INTEGER NOT NULL,
  started_at           TEXT NOT NULL,
  ended_at             TEXT,
  duration_ms          INTEGER,
  queue_wait_ms        INTEGER,
  llm_call_ms          INTEGER,
  ttft_ms              INTEGER,
  overhead_ms          INTEGER,
  model                TEXT,
  prompt_chars         INTEGER,
  prompt_tokens        INTEGER,
  cached_input_tokens  INTEGER,
  system_prompt_chars  INTEGER,
  completion_chars     INTEGER,
  completion_tokens    INTEGER,
  reasoning_tokens     INTEGER,
  total_tokens         INTEGER,
  cost_usd             REAL,
  prompt_excerpt       TEXT,
  completion_excerpt   TEXT,
  messages_json        TEXT,
  output_json          TEXT,
  outcome              TEXT,
  error_type           TEXT,
  tags                 TEXT,
  extra_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_turn ON node_trace(chat_id, turn_id, started_at);
CREATE INDEX IF NOT EXISTS idx_node      ON node_trace(node);
CREATE INDEX IF NOT EXISTS idx_started   ON node_trace(started_at);

CREATE VIEW IF NOT EXISTS turn_summary AS
  SELECT
    chat_id, case_id, turn_id,
    MIN(started_at) AS turn_started_at,
    MAX(ended_at)   AS turn_ended_at,
    SUM(prompt_tokens)       AS total_prompt_tokens,
    SUM(completion_tokens)   AS total_completion_tokens,
    SUM(cached_input_tokens) AS total_cached_tokens,
    SUM(reasoning_tokens)    AS total_reasoning_tokens,
    SUM(cost_usd)            AS total_cost_usd,
    COUNT(*)                 AS n_nodes,
    SUM(CASE WHEN depth = 1 THEN 1 ELSE 0 END) AS n_llm_rounds,
    SUM(CASE WHEN outcome IN ('failed','timeout') THEN 1 ELSE 0 END) AS n_failures
  FROM node_trace
  GROUP BY chat_id, case_id, turn_id;

CREATE VIEW IF NOT EXISTS session_summary AS
  SELECT
    chat_id, case_id,
    COUNT(DISTINCT turn_id) AS n_turns,
    SUM(prompt_tokens + completion_tokens) AS total_tokens,
    SUM(cost_usd) AS total_cost_usd,
    MIN(started_at) AS session_started_at,
    MAX(ended_at)   AS session_ended_at
  FROM node_trace
  GROUP BY chat_id, case_id;
"""


class NodeTraceStore:
    """Owns the SQLite connection + write lock for the node_trace table.

    Constructed once per process. Writes are serialized through ``_lock``;
    reads do not take the lock (WAL mode keeps readers non-blocking).

    Every public method swallows exceptions: telemetry must NEVER break
    an LLM call. Failures land as ``-1`` from ``insert`` and a no-op
    from ``update``; the first failure per process logs to stderr.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._failure_logged = False
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # check_same_thread=False so async callers from a worker thread
        # (safechain's asyncio.to_thread bridge) can write through the
        # same connection — protected by self._lock.
        self._conn = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    _ALLOWED_UPDATE_COLS = frozenset({
        "ended_at", "duration_ms", "queue_wait_ms", "llm_call_ms",
        "ttft_ms", "overhead_ms", "model",
        "prompt_chars", "prompt_tokens", "cached_input_tokens",
        "system_prompt_chars", "completion_chars", "completion_tokens",
        "reasoning_tokens", "total_tokens", "cost_usd",
        "prompt_excerpt", "completion_excerpt", "messages_json", "output_json",
        "outcome", "error_type", "tags", "extra_json",
    })

    def insert(
        self,
        *,
        chat_id: str,
        case_id: str,
        turn_id: str,
        node: str,
        parent_id: int | None,
        depth: int,
        started_at: str,
        model: str | None = None,
        extra_json: str | None = None,
    ) -> int:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO node_trace "
                    "(chat_id, case_id, turn_id, node, parent_id, depth, "
                    " started_at, model, extra_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, case_id, turn_id, node, parent_id, depth,
                     started_at, model, extra_json),
                )
                return int(cur.lastrowid or -1)
        except Exception as exc:  # noqa: BLE001
            self._log_failure("insert", exc)
            return -1

    def update(self, row_id: int, **fields: Any) -> None:
        if not fields or row_id <= 0:
            return
        # Whitelist columns to avoid trusting field names from callers.
        clean = {k: v for k, v in fields.items() if k in self._ALLOWED_UPDATE_COLS}
        if not clean:
            return
        try:
            cols = ", ".join(f"{k} = ?" for k in clean)
            params = list(clean.values()) + [row_id]
            with self._lock:
                self._conn.execute(
                    f"UPDATE node_trace SET {cols} WHERE id = ?", params,
                )
        except Exception as exc:  # noqa: BLE001
            self._log_failure("update", exc)

    def _log_failure(self, op: str, exc: Exception) -> None:
        if self._failure_logged:
            return
        self._failure_logged = True
        import sys
        print(
            f"[node_trace] DB {op} failed once "
            f"({type(exc).__name__}: {exc}); subsequent failures suppressed.",
            file=sys.stderr,
        )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_tools/test_node_trace/test_core.py -v`
Expected: 3 PASSED.

- [ ] **Step 6: Commit (offer; do not auto-run)**

```
git add tools/node_trace/__init__.py tools/node_trace/core.py tests/test_tools/node_trace/__init__.py tests/test_tools/test_node_trace/test_core.py
git commit -m "feat(node_trace): add SQLite-backed NodeTraceStore"
```

---

### Task 3: NodeTrace async context manager + ACTIVE_NODE contextvar + attach_usage helper

**Files:**
- Modify: `tools/node_trace/core.py` (add classes/helpers below `NodeTraceStore`)
- Test: `tests/test_tools/test_node_trace/test_core.py` (append)

- [ ] **Step 1: Write the failing test (append to test file)**

Append to `tests/test_tools/test_node_trace/test_core.py`:

```python
import asyncio
import sqlite3

from tools.node_trace import NodeTrace, NodeTraceStore, attach_usage


def test_nested_node_traces_parent_chain(tmp_path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))

    async def run():
        async with NodeTrace(
            store, chat_id="c", case_id="c", turn_id="t",
            node="specialist.spend_payments", depth=0,
        ) as outer:
            async with NodeTrace(
                store, chat_id="c", case_id="c", turn_id="t",
                node="specialist.spend_payments.round_1", depth=1,
            ) as inner:
                attach_usage(
                    prompt_tokens=100, completion_tokens=20,
                    prompt_excerpt="hi", completion_excerpt="ok",
                    model="gpt-test",
                )
        return outer.row_id, inner.row_id

    outer_id, inner_id = asyncio.run(run())

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    parent = conn.execute(
        "SELECT id, parent_id, outcome FROM node_trace WHERE id = ?",
        (outer_id,),
    ).fetchone()
    child = conn.execute(
        "SELECT id, parent_id, outcome, prompt_tokens, completion_tokens, model "
        "FROM node_trace WHERE id = ?",
        (inner_id,),
    ).fetchone()
    assert parent == (outer_id, None, "ok")
    assert child == (inner_id, outer_id, "ok", 100, 20, "gpt-test")


def test_node_trace_records_failure(tmp_path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))

    async def run():
        try:
            async with NodeTrace(
                store, chat_id="c", case_id="c", turn_id="t",
                node="chat.redact", depth=0,
            ) as nt:
                raise ValueError("boom")
        except ValueError:
            pass
        return nt.row_id

    row_id = asyncio.run(run())
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    row = conn.execute(
        "SELECT outcome, error_type FROM node_trace WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row == ("failed", "ValueError")


def test_attach_usage_noop_without_active_node():
    # No active NodeTrace on the contextvar — attach_usage just returns.
    attach_usage(prompt_tokens=1, completion_tokens=1)  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_tools/test_node_trace/test_core.py::test_nested_node_traces_parent_chain -v`
Expected: FAIL with `ImportError: cannot import name 'NodeTrace'`

- [ ] **Step 3: Append NodeTrace + ACTIVE_NODE + attach_usage to tools/node_trace/core.py**

Append to `tools/node_trace/core.py`:

```python
import contextvars
from datetime import datetime, timezone
from time import perf_counter


ACTIVE_NODE: contextvars.ContextVar["NodeTrace | None"] = contextvars.ContextVar(
    "ACTIVE_NODE", default=None,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(text: str, head: int | None = None, tail: int | None = None) -> str:
    """Return head + ' … <N elided> … ' + tail. Configurable via env."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if os.environ.get("NODE_TRACE_FULL_PROMPT") == "1":
        return text
    h = head if head is not None else int(os.environ.get("NODE_TRACE_EXCERPT_HEAD", "400"))
    t = tail if tail is not None else int(os.environ.get("NODE_TRACE_EXCERPT_TAIL", "200"))
    if len(text) <= h + t:
        return text
    elided = len(text) - h - t
    return f"{text[:h]} … <{elided} chars elided> … {text[-t:]}"


class NodeTrace:
    """Async context manager wrapping one node in the reasoning trace.

    On enter: INSERT a row with started_at + node + depth + parent_id
    (resolved from ACTIVE_NODE). Pushes self onto ACTIVE_NODE.

    On exit: UPDATE the row with ended_at + duration_ms + outcome +
    accumulated prompt/completion fields. Pops the contextvar.

    Accumulators (set via attach_usage / attach_tag / attach_latency /
    attach_io from LLM clients + firewall_stack):
        token counts:    prompt_tokens, completion_tokens, total_tokens,
                         cached_input_tokens, reasoning_tokens
        sizing:          prompt_chars, system_prompt_chars, completion_chars
        excerpts + I/O:  prompt_excerpt, completion_excerpt, messages_json,
                         output_json
        latency split:   queue_wait_ms, llm_call_ms, ttft_ms (overhead_ms
                         computed at __aexit__)
        meta:            model, cost_usd, tags (list[str])
    """

    def __init__(
        self,
        store: NodeTraceStore,
        *,
        chat_id: str,
        case_id: str,
        turn_id: str,
        node: str,
        depth: int,
        extra_json: str | None = None,
    ) -> None:
        self._store = store
        self.chat_id = chat_id
        self.case_id = case_id
        self.turn_id = turn_id
        self.node = node
        self.depth = depth
        self._extra_json = extra_json
        self.row_id: int = -1
        self._token: contextvars.Token | None = None
        self._t0: float = 0.0
        self._round_count: int = 0
        # Token-count accumulators
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0
        self.cached_input_tokens: int = 0
        self.reasoning_tokens: int = 0
        # Sizing
        self.prompt_chars: int = 0
        self.system_prompt_chars: int = 0
        self.completion_chars: int = 0
        # Excerpts + full I/O
        self.prompt_excerpt: str = ""
        self.completion_excerpt: str = ""
        self.messages_json: str | None = None
        self.output_json: str | None = None
        # Latency split (filled in by attach_latency)
        self.queue_wait_ms: int | None = None
        self.llm_call_ms: int | None = None
        self.ttft_ms: int | None = None
        # Meta
        self.model: str | None = None
        self.cost_usd: float = 0.0
        self.tags: list[str] = []

    @property
    def parent(self) -> "NodeTrace | None":
        # Resolve current top-of-stack BEFORE we push ourselves on enter.
        return ACTIVE_NODE.get()

    def next_round_index(self) -> int:
        self._round_count += 1
        return self._round_count

    async def __aenter__(self) -> "NodeTrace":
        parent = ACTIVE_NODE.get()
        self._t0 = perf_counter()
        self.row_id = self._store.insert(
            chat_id=self.chat_id,
            case_id=self.case_id,
            turn_id=self.turn_id,
            node=self.node,
            parent_id=parent.row_id if parent and parent.row_id > 0 else None,
            depth=self.depth,
            started_at=_now_iso(),
            extra_json=self._extra_json,
        )
        self._token = ACTIVE_NODE.set(self)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            duration_ms = int((perf_counter() - self._t0) * 1000)
            outcome = "ok" if exc_type is None else "failed"
            # Derived: overhead = duration - queue_wait - llm_call (clamped >= 0)
            overhead_ms: int | None = None
            if self.queue_wait_ms is not None or self.llm_call_ms is not None:
                accounted = (self.queue_wait_ms or 0) + (self.llm_call_ms or 0)
                overhead_ms = max(0, duration_ms - accounted)
            tags_payload = json.dumps(self.tags) if self.tags else None
            self._store.update(
                self.row_id,
                ended_at=_now_iso(),
                duration_ms=duration_ms,
                queue_wait_ms=self.queue_wait_ms,
                llm_call_ms=self.llm_call_ms,
                ttft_ms=self.ttft_ms,
                overhead_ms=overhead_ms,
                outcome=outcome,
                error_type=exc_type.__name__ if exc_type else None,
                model=self.model,
                prompt_tokens=self.prompt_tokens or None,
                completion_tokens=self.completion_tokens or None,
                cached_input_tokens=self.cached_input_tokens or None,
                reasoning_tokens=self.reasoning_tokens or None,
                total_tokens=self.total_tokens or None,
                cost_usd=self.cost_usd or None,
                prompt_chars=self.prompt_chars or None,
                system_prompt_chars=self.system_prompt_chars or None,
                completion_chars=self.completion_chars or None,
                prompt_excerpt=self.prompt_excerpt or None,
                completion_excerpt=self.completion_excerpt or None,
                messages_json=self.messages_json,
                output_json=self.output_json,
                tags=tags_payload,
            )
        finally:
            if self._token is not None:
                ACTIVE_NODE.reset(self._token)
                self._token = None
        # Don't suppress the exception.
        return None


def attach_usage(
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    prompt_excerpt: str | None = None,
    completion_excerpt: str | None = None,
    prompt_chars: int | None = None,
    system_prompt_chars: int | None = None,
    completion_chars: int | None = None,
    model: str | None = None,
    cost_usd: float | None = None,
) -> None:
    """Write usage/excerpt fields onto the ACTIVE_NODE if one is set.

    No-op when there is no active node — callers in untraced code paths
    don't have to guard.
    """
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    if prompt_tokens is not None:
        nt.prompt_tokens = prompt_tokens
    if completion_tokens is not None:
        nt.completion_tokens = completion_tokens
    if total_tokens is not None:
        nt.total_tokens = total_tokens
    elif prompt_tokens is not None or completion_tokens is not None:
        nt.total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    if cached_input_tokens is not None:
        nt.cached_input_tokens = cached_input_tokens
    if reasoning_tokens is not None:
        nt.reasoning_tokens = reasoning_tokens
    if prompt_excerpt is not None:
        nt.prompt_excerpt = _excerpt(prompt_excerpt)
        nt.prompt_chars = len(prompt_excerpt)
    if prompt_chars is not None:
        nt.prompt_chars = prompt_chars
    if system_prompt_chars is not None:
        nt.system_prompt_chars = system_prompt_chars
    if completion_excerpt is not None:
        nt.completion_excerpt = _excerpt(completion_excerpt)
        nt.completion_chars = len(completion_excerpt)
    if completion_chars is not None:
        nt.completion_chars = completion_chars
    if model is not None:
        nt.model = model
    if cost_usd is not None:
        nt.cost_usd = cost_usd


def attach_tag(*tags: str) -> None:
    """Append tag(s) to the active node. No-op when no active node."""
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    for t in tags:
        if t and t not in nt.tags:
            nt.tags.append(t)


def attach_latency(
    *,
    queue_wait_ms: int | None = None,
    llm_call_ms: int | None = None,
    ttft_ms: int | None = None,
) -> None:
    """Record per-segment latency on the active node. Each call is
    additive for queue_wait_ms (one node may go through the semaphore
    multiple times on retry) and overwriting for llm_call_ms / ttft_ms."""
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    if queue_wait_ms is not None:
        nt.queue_wait_ms = (nt.queue_wait_ms or 0) + queue_wait_ms
    if llm_call_ms is not None:
        nt.llm_call_ms = llm_call_ms
    if ttft_ms is not None and nt.ttft_ms is None:
        # First-token only — don't overwrite if it's already set.
        nt.ttft_ms = ttft_ms


def attach_io(
    *,
    messages_json: str | None = None,
    output_json: str | None = None,
) -> None:
    """Store the full structured input / output, gated by
    NODE_TRACE_STORE_FULL_IO=1. Off by default to keep DB size sane."""
    if os.environ.get("NODE_TRACE_STORE_FULL_IO") != "1":
        return
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    if messages_json is not None:
        nt.messages_json = messages_json
    if output_json is not None:
        nt.output_json = output_json
```

Also add `import json` to the top of `tools/node_trace/core.py` (needed by `__aexit__` and callers passing structured payloads).

- [ ] **Step 4: Extend `tools/node_trace/__init__.py` re-exports**

Replace `tools/node_trace/__init__.py` content with the broader public surface now that Task 3's symbols exist:

```python
"""Per-node trace package: SQLite-backed observability for every LLM call boundary."""
from tools.node_trace.core import (
    ACTIVE_NODE,
    NodeTrace,
    NodeTraceStore,
    TURN_SCOPE,
    TurnScope,
    attach_extra,
    attach_io,
    attach_latency,
    attach_tag,
    attach_usage,
    _open_node,
)

__all__ = [
    "ACTIVE_NODE",
    "NodeTrace",
    "NodeTraceStore",
    "TURN_SCOPE",
    "TurnScope",
    "attach_extra",
    "attach_io",
    "attach_latency",
    "attach_tag",
    "attach_usage",
    "_open_node",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_tools/test_node_trace/test_core.py -v`
Expected: 6 PASSED (3 from Task 2 + 3 new).

- [ ] **Step 6: Commit (offer)**

```
git add tools/node_trace/__init__.py tools/node_trace/core.py tests/test_tools/test_node_trace/test_core.py
git commit -m "feat(node_trace): add NodeTrace context manager + active-node contextvar"
```

---

### Task 4 (NEW): Pricing table — compute cost_usd from tokens

**Files:**
- Create: `tools/node_trace/pricing.py`
- Test: `tests/test_tools/test_node_trace/test_pricing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools/test_node_trace/test_pricing.py`:

```python
from tools.node_trace.pricing import compute_cost


def test_known_model_cost_basic():
    # gpt-4o-mini per million tokens: input $0.15, output $0.60
    # 1000 prompt + 500 completion = 0.00015 + 0.00030 = 0.00045
    cost = compute_cost(
        model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500,
    )
    assert abs(cost - 0.00045) < 1e-9


def test_cached_tokens_discount():
    # gpt-4o-mini cached input: $0.075 / 1M (half of fresh)
    # If 1000 prompt of which 400 are cached, cost = 600*0.15/1e6 + 400*0.075/1e6
    cost = compute_cost(
        model="gpt-4o-mini",
        prompt_tokens=1000,
        cached_input_tokens=400,
        completion_tokens=0,
    )
    expected = 600 * 0.15 / 1_000_000 + 400 * 0.075 / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_unknown_model_returns_zero_no_raise():
    assert compute_cost(model="some-weird-model", prompt_tokens=1, completion_tokens=1) == 0.0


def test_none_model_returns_zero():
    assert compute_cost(model=None, prompt_tokens=1000) == 0.0
```

- [ ] **Step 2: Run the test — expect failure**

Run: `pytest tests/test_tools/test_node_trace/test_pricing.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement tools/node_trace/pricing.py**

Create `tools/node_trace/pricing.py`:

```python
"""Per-model $/1M-token price table used by NodeTrace cost_usd column.

Numbers reflect OpenAI's published list at plan-write time (2026-05-21).
Updates: edit `_PRICES`. Unknown models cost 0 (cost field stays at 0
so downstream sums aren't poisoned by guesses).
"""
from __future__ import annotations

# Per-million-token rates (input, cached_input, output).
# cached_input defaults to half the input rate when not explicitly listed.
_PRICES: dict[str, tuple[float, float | None, float]] = {
    "gpt-4o":             (2.50, 1.25, 10.00),
    "gpt-4o-mini":        (0.15, 0.075, 0.60),
    "gpt-4-turbo":        (10.00, None, 30.00),
    "gpt-4":              (30.00, None, 60.00),
    "gpt-3.5-turbo":      (0.50, None, 1.50),
    "o1":                 (15.00, 7.50, 60.00),
    "o1-mini":            (3.00, 1.50, 12.00),
    "o3-mini":            (1.10, 0.55, 4.40),
}


def _normalize_model(model: str) -> str:
    # OpenAI returns model IDs like "gpt-4o-2024-08-06". Strip the date
    # suffix so the table stays small.
    m = model.lower()
    for prefix in _PRICES:
        if m.startswith(prefix):
            return prefix
    return m


def compute_cost(
    *,
    model: str | None,
    prompt_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> float:
    if not model:
        return 0.0
    key = _normalize_model(model)
    if key not in _PRICES:
        return 0.0
    rate_in, rate_cached, rate_out = _PRICES[key]
    if rate_cached is None:
        rate_cached = rate_in / 2
    p_in = prompt_tokens or 0
    p_cached = min(cached_input_tokens or 0, p_in)
    p_fresh = p_in - p_cached
    p_out = completion_tokens or 0
    return (
        p_fresh * rate_in / 1_000_000
        + p_cached * rate_cached / 1_000_000
        + p_out * rate_out / 1_000_000
    )
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_tools/test_node_trace/test_pricing.py -v`
Expected: 4 PASSED.

- [ ] **Step 5: Commit (offer)**

```
git add tools/node_trace/pricing.py tests/test_tools/test_node_trace/test_pricing.py
git commit -m "feat(node_trace): add per-model token-to-dollar pricing table"
```

---

### Task 5: Wire firewall_client.py — capture OpenAI usage + create round rows

**Files:**
- Modify: `llm/firewall_client.py`
- Test: `tests/test_tools/test_node_trace/test_firewall_hook.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools/test_node_trace/test_firewall_hook.py`:

```python
import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from tools.node_trace import NodeTrace, NodeTraceStore


@pytest.mark.asyncio
async def test_firewall_client_creates_round_under_parent(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    # Stub the inner OpenAI client.
    inner = MagicMock()
    inner.chat = SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
            usage=SimpleNamespace(
                prompt_tokens=42,
                completion_tokens=7,
                total_tokens=49,
            ),
            model="gpt-4o-mini",
        ))
    ))
    logger = EventLogger(session_id="t", log_dir=str(tmp_path))
    firewall = FirewallStack(logger=logger)
    client = FirewalledAsyncOpenAI(base=inner, firewall=firewall)

    async with NodeTrace(
        store, chat_id="c", case_id="c", turn_id="t",
        node="specialist.spend_payments", depth=0,
    ) as parent:
        await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    rows = conn.execute(
        "SELECT node, parent_id, depth, prompt_tokens, completion_tokens, model "
        "FROM node_trace ORDER BY id"
    ).fetchall()
    # Parent row + one round child.
    assert rows[0][0] == "specialist.spend_payments"
    assert rows[1] == ("specialist.spend_payments.round_1", parent.row_id, 1,
                       42, 7, "gpt-4o-mini")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_tools/test_node_trace/test_firewall_hook.py -v`
Expected: FAIL — the firewall client doesn't yet create round rows.

- [ ] **Step 3: Modify llm/firewall_client.py**

Replace the body of `_FirewalledChatCompletions.create`:

```python
async def create(self, *, model, messages, **kw):
    from tools.node_trace import (
        ACTIVE_NODE, NodeTrace, attach_io, attach_latency, attach_usage,
    )
    import json
    # node_trace.NodeTraceStore lives on firewall via SessionClients; we
    # do not depend on it here — we only PARTICIPATE in an existing
    # outer NodeTrace by inserting a child round row when one is active.

    messages = [_redact_message(m) for m in messages]
    attempt = 0
    while True:
        try:
            async with self._firewall.gate():
                parent = ACTIVE_NODE.get()
                if parent is None or parent.row_id <= 0:
                    # No parent — code path was not wired up with a
                    # NodeTrace. Just call through, no telemetry.
                    return await self._base.create(
                        model=model, messages=messages, **kw,
                    )
                round_idx = parent.next_round_index()
                async with NodeTrace(
                    store=parent._store,
                    chat_id=parent.chat_id,
                    case_id=parent.case_id,
                    turn_id=parent.turn_id,
                    node=f"{parent.node}.round_{round_idx}",
                    depth=parent.depth + 1,
                ) as nt:
                    from tools.node_trace.pricing import compute_cost
                    nt.model = model
                    prompt_text = _render_messages_for_excerpt(messages)
                    sys_chars = sum(
                        len(m.get("content") or "")
                        for m in messages if m.get("role") == "system"
                    )
                    attach_usage(
                        prompt_excerpt=prompt_text,
                        system_prompt_chars=sys_chars or None,
                        model=model,
                    )
                    attach_io(messages_json=json.dumps(messages, default=str))
                    import json as _json
                    from time import perf_counter as _pc
                    _llm_t0 = _pc()
                    resp = await self._base.create(
                        model=model, messages=messages, **kw,
                    )
                    attach_latency(llm_call_ms=int((_pc() - _llm_t0) * 1000))
                    usage = getattr(resp, "usage", None)
                    if usage is not None:
                        # Top-level token counts
                        p_tok = getattr(usage, "prompt_tokens", None)
                        c_tok = getattr(usage, "completion_tokens", None)
                        t_tok = getattr(usage, "total_tokens", None)
                        # Prompt-cache detail (late-2024+ models). Field
                        # may be a dict or a pydantic model on the SDK
                        # version; getattr+dict-fallback handles both.
                        pdet = getattr(usage, "prompt_tokens_details", None)
                        cached = None
                        if pdet is not None:
                            cached = (
                                getattr(pdet, "cached_tokens", None)
                                if not isinstance(pdet, dict)
                                else pdet.get("cached_tokens")
                            )
                        # Reasoning detail (o1/o3)
                        cdet = getattr(usage, "completion_tokens_details", None)
                        reasoning = None
                        if cdet is not None:
                            reasoning = (
                                getattr(cdet, "reasoning_tokens", None)
                                if not isinstance(cdet, dict)
                                else cdet.get("reasoning_tokens")
                            )
                        cost = compute_cost(
                            model=model,
                            prompt_tokens=p_tok,
                            cached_input_tokens=cached,
                            completion_tokens=c_tok,
                        )
                        attach_usage(
                            prompt_tokens=p_tok,
                            completion_tokens=c_tok,
                            total_tokens=t_tok,
                            cached_input_tokens=cached,
                            reasoning_tokens=reasoning,
                            cost_usd=cost,
                        )
                    try:
                        completion_text = (
                            resp.choices[0].message.content or ""
                            if getattr(resp, "choices", None) else ""
                        )
                    except Exception:
                        completion_text = ""
                    attach_usage(completion_excerpt=completion_text)
                    try:
                        out_json = resp.model_dump_json() if hasattr(resp, "model_dump_json") else None
                    except Exception:
                        out_json = None
                    if out_json is not None:
                        attach_io(output_json=out_json)
                    return resp
        except FirewallRejection as e:
            self._firewall.logger.log("firewall_rejection",
                                      {"code": e.code, "message": e.message,
                                       "attempt": attempt})
            if attempt >= self._firewall.max_retries:
                self._firewall.logger.log("firewall_blocked",
                                          {"code": e.code, "message": e.message,
                                           "attempts": attempt + 1})
                raise
            attempt += 1
            messages = _inject_guidance(messages)


def _render_messages_for_excerpt(messages: list[dict]) -> str:
    """Flatten the message list into one readable string for the excerpt
    field. Same shape as a chat transcript, just for human-readability in
    the DB."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        parts.append(f"[{role}] {content}")
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_tools/test_node_trace/test_firewall_hook.py -v`
Expected: 1 PASSED.

- [ ] **Step 5: Run the existing firewall tests to check nothing broke**

Run: `pytest tests/ -k firewall -v`
Expected: existing firewall-related tests still pass (no behavioral regressions when there's no active parent NodeTrace).

- [ ] **Step 6: Commit (offer)**

```
git add llm/firewall_client.py tests/test_tools/test_node_trace/test_firewall_hook.py
git commit -m "feat(node_trace): wire OpenAI client to emit round rows + usage"
```

---

### Task 6: Wire safechain_client.py — tiktoken estimate + round rows

**Files:**
- Modify: `llm/safechain_client.py`
- Test: `tests/test_tools/test_node_trace/test_safechain_hook.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools/test_node_trace/test_safechain_hook.py`:

```python
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from llm.firewall_stack import FirewallStack
from llm.safechain_client import SafeChainAsyncOpenAI
from logger.event_logger import EventLogger
from tools.node_trace import NodeTrace, NodeTraceStore


@pytest.mark.asyncio
async def test_safechain_round_uses_tiktoken_estimate(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    logger = EventLogger(session_id="t", log_dir=str(tmp_path))
    firewall = FirewallStack(logger=logger)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o-mini", firewall=firewall)

    # Patch the underlying _do_invoke to bypass real safechain.
    async def _fake_invoke(*a, **k):
        return '{"output": {"answer": "ok"}}'
    with patch.object(
        client.chat.completions, "_invoke", new=AsyncMock(side_effect=_fake_invoke)
    ):
        # The patched method short-circuits the real invoke; we just want
        # the wrapping NodeTrace + attach_usage path to fire.
        pass

    # NOTE: real test wires through the actual `_invoke` after our edits
    # add attach_usage there. The integration test below catches that.
```

(Marker test only — Task 5's real assertion is verified by Task 10's integration test. Keeping a placeholder file in case we need targeted safechain isolation later.)

Actually replace the above with a simpler structural assertion:

```python
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from llm.firewall_stack import FirewallStack
from llm.safechain_client import SafeChainAsyncOpenAI, _SafeChainChatCompletions
from logger.event_logger import EventLogger
from tools.node_trace import NodeTrace, NodeTraceStore


@pytest.mark.asyncio
async def test_safechain_create_records_round_with_tiktoken(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    logger = EventLogger(session_id="t", log_dir=str(tmp_path))
    firewall = FirewallStack(logger=logger)
    client = SafeChainAsyncOpenAI(model_name="gpt-4o-mini", firewall=firewall)

    # Bypass real safechain by stubbing _invoke to return a synthetic
    # ChatCompletion-shaped object — easier than mocking the LCEL chain.
    async def _stub_invoke(self_, *, model, messages, tools, response_format, stream):
        from llm.safechain_client import _synthesize_chat_completion
        return _synthesize_chat_completion(text='{"output":"hi"}', model=model)

    with patch.object(_SafeChainChatCompletions, "_invoke", _stub_invoke):
        async with NodeTrace(
            store, chat_id="c", case_id="c", turn_id="t",
            node="specialist.modeling", depth=0,
        ):
            await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "estimate me"}],
            )

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    rows = conn.execute(
        "SELECT node, prompt_tokens, completion_tokens FROM node_trace ORDER BY id"
    ).fetchall()
    assert rows[0][0] == "specialist.modeling"
    assert rows[1][0] == "specialist.modeling.round_1"
    # tiktoken estimate > 0 for a non-empty prompt.
    assert rows[1][1] > 0
    assert rows[1][2] > 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_tools/test_node_trace/test_safechain_hook.py -v`
Expected: FAIL — no round row created yet.

- [ ] **Step 3: Modify llm/safechain_client.py**

In `_SafeChainChatCompletions.create`, wrap the gate body. Replace the gate block:

```python
async with firewall.gate():
    return await self._invoke(
        model=model,
        messages=messages,
        tools=tools,
        response_format=response_format,
        stream=stream,
    )
```

with:

```python
from tools.node_trace import (
    ACTIVE_NODE, NodeTrace, attach_io, attach_latency, attach_usage,
)
from tools.node_trace.pricing import compute_cost
from time import perf_counter as _pc
import json as _json

async with firewall.gate():
    parent = ACTIVE_NODE.get()
    if parent is None or parent.row_id <= 0:
        return await self._invoke(
            model=model, messages=messages, tools=tools,
            response_format=response_format, stream=stream,
        )
    round_idx = parent.next_round_index()
    async with NodeTrace(
        store=parent._store,
        chat_id=parent.chat_id,
        case_id=parent.case_id,
        turn_id=parent.turn_id,
        node=f"{parent.node}.round_{round_idx}",
        depth=parent.depth + 1,
    ):
        combined = _combine_messages(messages, tools, response_format)
        sys_chars = sum(
            len(m.get("content") or "")
            for m in messages if m.get("role") == "system"
        )
        p_tok = _estimate_tokens(combined, model)
        attach_usage(
            prompt_excerpt=combined,
            prompt_tokens=p_tok,
            system_prompt_chars=sys_chars or None,
            model=model,
        )
        attach_io(messages_json=_json.dumps(messages, default=str))
        _llm_t0 = _pc()
        resp = await self._invoke(
            model=model, messages=messages, tools=tools,
            response_format=response_format, stream=stream,
        )
        attach_latency(llm_call_ms=int((_pc() - _llm_t0) * 1000))
        try:
            if hasattr(resp, "choices") and resp.choices:
                completion_text = resp.choices[0].message.content or ""
            else:
                completion_text = ""
        except Exception:
            completion_text = ""
        c_tok = _estimate_tokens(completion_text, model)
        attach_usage(
            completion_excerpt=completion_text,
            completion_tokens=c_tok,
            cost_usd=compute_cost(
                model=model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
            ),
        )
        return resp
```

Add module-level helper near the top of `safechain_client.py`:

```python
def _estimate_tokens(text: str, model: str) -> int:
    """tiktoken estimate with a robust fallback. Safechain doesn't return
    usage objects, so this is the only token signal we have on that path."""
    if not text:
        return 0
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Last-resort: 4 chars per token. Marked in extra_json by the caller
        # when this branch hits — but tiktoken is in requirements.txt so
        # this should be rare.
        return max(1, len(text) // 4)
```

Make sure the new import is placed inside the method (not at module top) to avoid an import cycle with `firewall_stack.py` when `node_trace.py` later grows imports.

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_tools/test_node_trace/test_safechain_hook.py -v`
Expected: PASSED.

- [ ] **Step 5: Commit (offer)**

```
git add llm/safechain_client.py tests/test_tools/test_node_trace/test_safechain_hook.py
git commit -m "feat(node_trace): wire safechain client with tiktoken-estimated rounds"
```

---

### Task 7 (NEW): Wire firewall_stack.gate — record queue_wait_ms onto active node

**Files:**
- Modify: `llm/firewall_stack.py`
- Test: append to `tests/test_tools/test_node_trace/test_firewall_hook.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tools/test_node_trace/test_firewall_hook.py`:

```python
@pytest.mark.asyncio
async def test_firewall_gate_records_queue_wait(tmp_path: Path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))
    logger = EventLogger(session_id="t", log_dir=str(tmp_path))
    firewall = FirewallStack(
        logger=logger, specialist_concurrency=1, orchestrator_concurrency=1,
    )
    # Force a meaningful queue wait by pre-acquiring the orchestrator semaphore.
    await firewall.orchestrator_semaphore.acquire()

    from tools.node_trace import NodeTrace

    async def waiter_task():
        async with NodeTrace(
            store, chat_id="c", case_id="c", turn_id="t",
            node="orchestrator", depth=0,
        ) as nt:
            async with firewall.gate():
                pass
            return nt.row_id

    task = asyncio.create_task(waiter_task())
    await asyncio.sleep(0.15)
    firewall.orchestrator_semaphore.release()
    row_id = await task

    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    qw = conn.execute(
        "SELECT queue_wait_ms FROM node_trace WHERE id = ?", (row_id,)
    ).fetchone()[0]
    assert qw is not None and qw >= 100
```

- [ ] **Step 2: Run the test — expect failure**

Run: `pytest tests/test_tools/test_node_trace/test_firewall_hook.py::test_firewall_gate_records_queue_wait -v`
Expected: FAIL — gate doesn't write to the active node yet.

- [ ] **Step 3: Modify llm/firewall_stack.py `gate()`**

In the `gate()` async context manager, after the `waited_ms = int((time.perf_counter() - t0) * 1000)` line, also push it onto the active node:

```python
# Inside FirewallStack.gate(), right after waited_ms is computed:
try:
    from tools.node_trace import attach_latency
    attach_latency(queue_wait_ms=waited_ms)
except Exception:
    # Telemetry must never break a real LLM call.
    pass
```

Keep the existing `firewall_semaphore_wait` log call as-is (it's a separate signal stream that we don't want to lose).

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_tools/test_node_trace/test_firewall_hook.py::test_firewall_gate_records_queue_wait -v`
Expected: PASSED.

- [ ] **Step 5: Commit (offer)**

```
git add llm/firewall_stack.py tests/test_tools/test_node_trace/test_firewall_hook.py
git commit -m "feat(node_trace): record semaphore queue_wait_ms on active node"
```

---

### Task 8: Wire chat_agent.py — depth-0 nodes around redact / relevance_check / clarify_intent

**Files:**
- Modify: `agent_factories/chat_agent.py`
- Test: covered by Task 10 integration smoke

- [ ] **Step 1: Inspect current chat_agent surface**

`chat_agent.py` has three LLM-calling methods: `redact`, `relevance_check`, `clarify_intent`. Each makes one `self.llm.ainvoke(...)` call, which lands in the `FirewalledChatShim` and hits the wrapped LLM client. We need a depth-0 wrapper at each method so the resulting round row is correctly parented under the right logical name.

The `ChatAgent` needs access to a `NodeTraceStore` + the active `chat_id` / `case_id` / `turn_id`. The cleanest wiring: take the store as a constructor argument (optional, default None), and resolve chat/case/turn via a new contextvar-based "TURN_SCOPE" set by `server.py` for each turn.

- [ ] **Step 2: Add TURN_SCOPE contextvar in tools/node_trace/core.py**

Append to `tools/node_trace/core.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnScope:
    chat_id: str
    case_id: str
    turn_id: str


TURN_SCOPE: contextvars.ContextVar[TurnScope | None] = contextvars.ContextVar(
    "TURN_SCOPE", default=None,
)


def current_turn_scope() -> TurnScope | None:
    return TURN_SCOPE.get()


def _open_node(store: NodeTraceStore | None, node: str, depth: int = 0):
    """Convenience wrapper: build a NodeTrace from TURN_SCOPE + store.

    Returns a no-op async context manager when either is missing — call
    sites stay clean.
    """
    scope = TURN_SCOPE.get()
    if store is None or scope is None:
        return _NullNode()
    return NodeTrace(
        store=store,
        chat_id=scope.chat_id,
        case_id=scope.case_id,
        turn_id=scope.turn_id,
        node=node,
        depth=depth,
    )


class _NullNode:
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): return None
```

- [ ] **Step 3: Add a test for TurnScope + _open_node**

Append to `tests/test_tools/test_node_trace/test_core.py`:

```python
import asyncio
import sqlite3

from tools.node_trace import (
    NodeTraceStore, TURN_SCOPE, TurnScope, _open_node,
)


def test_open_node_uses_turn_scope(tmp_path):
    store = NodeTraceStore(str(tmp_path / "traces.db"))

    async def run():
        TURN_SCOPE.set(TurnScope(chat_id="ABC", case_id="X", turn_id="T1"))
        async with _open_node(store, "chat.redact", depth=0):
            pass

    asyncio.run(run())
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    row = conn.execute(
        "SELECT chat_id, case_id, turn_id, node, depth FROM node_trace"
    ).fetchone()
    assert row == ("ABC", "X", "T1", "chat.redact", 0)


def test_open_node_noop_without_store(tmp_path):
    # Must not raise even when called outside any turn scope.
    async def run():
        async with _open_node(None, "x", depth=0):
            pass
    asyncio.run(run())  # no exception = pass
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_tools/test_node_trace/test_core.py::test_open_node_uses_turn_scope tests/test_tools/test_node_trace/test_core.py::test_open_node_noop_without_store -v`
Expected: PASSED.

- [ ] **Step 5: Modify ChatAgent to accept + use the store**

In `agent_factories/chat_agent.py`:

1. Import: `from tools.node_trace import _open_node, NodeTraceStore`
2. Add `node_trace_store: NodeTraceStore | None = None` to `ChatAgent.__init__` and store on `self._node_trace_store`.
3. Wrap each of `redact`, `relevance_check`, `clarify_intent` bodies with `async with _open_node(self._node_trace_store, "chat.<method_name>"):` around the existing `self.llm.ainvoke(...)` call.

Concrete edit for `redact` (others follow the same pattern):

```python
async def redact(self, text: str) -> str:
    async with _open_node(self._node_trace_store, "chat.redact"):
        result = await self.llm.ainvoke(
            system_prompt=self._redact_prompt,
            user_message=(
                f"Text to redact:\n\n{text}\n\n"
                "Return JSON with redacted + masked_spans."
            ),
            json_mode=True,
        )
        if result.status == "blocked" or result.data is None:
            self.logger.log("chat_redact_fallback", {"reason": "blocked"})
            return text
        return str(result.data.get("redacted", text)) or text
```

Apply the same wrap around `relevance_check` (node `"chat.relevance_check"`) and `clarify_intent` (node `"chat.clarify_intent"`).

- [ ] **Step 6: Run all chat_agent tests**

Run: `pytest tests/test_agent_factories/test_chat_agent.py -v`
Expected: existing tests still pass (the wrapper is a no-op when store is None, which all existing tests provide).

- [ ] **Step 7: Commit (offer)**

```
git add tools/node_trace/core.py agent_factories/chat_agent.py tests/test_tools/test_node_trace/test_core.py
git commit -m "feat(node_trace): wrap ChatAgent.redact/relevance_check/clarify_intent"
```

---

### Task 9: Wire redacting_tool.py — depth-0 specialist + distiller nodes + cache/digest tags

**Files:**
- Modify: `agent_factories/redacting_tool.py`
- Test: covered by Task 10 integration smoke

- [ ] **Step 1: Add store to AppContext shape and wrap the specialist Runner.run**

`agent_factories/redacting_tool.py` builds tools via `_make_specialist_tool(...)`. The inner `_runner` is the function called per specialist invocation. Read `app_ctx._node_trace_store` (set by `server.py` in Task 8) and wrap the `Runner.run(inner, run_input, ...)` block with `_open_node(store, f"specialist.{name}", depth=0)`.

Concrete edit in `_runner` (around lines 460-485 of `redacting_tool.py`):

```python
from tools.node_trace import _open_node  # add to top-of-file imports

# ... inside _runner, replace the `try: t0 = time.perf_counter()` block:
try:
    t0 = time.perf_counter()
    kind_token = LLM_CALL_KIND.set("specialist")
    node_store = getattr(app_ctx, "_node_trace_store", None)
    try:
        async with _open_node(node_store, f"specialist.{name}", depth=0):
            result = await asyncio.wait_for(
                Runner.run(
                    inner, run_input, context=app_ctx,
                    max_turns=_SPECIALIST_MAX_TURNS,
                ),
                timeout=_SPECIALIST_TIMEOUT_S,
            )
    finally:
        LLM_CALL_KIND.reset(kind_token)
    timer.record(...)  # unchanged
```

Apply the same treatment to the distiller `Runner.run` block in `_distill_and_persist` (around line 218): wrap it with `async with _open_node(node_store, f"distiller.{name}", depth=0):`. Read `node_store` from `app_ctx._node_trace_store`.

- [ ] **Step 2: Tag dedup hits + KB digest presence on the active node**

In `_runner`, when the per-AppContext dedup cache returns a cached payload (around line 412 of `redacting_tool.py`), tag the active node and short-circuit:

```python
from tools.node_trace import attach_tag  # add to top-of-file imports
# ... inside the dedup-hit branch:
if isinstance(seen, dict) and cache_key in seen:
    cached = seen[cache_key]
    if logger is not None:
        logger.log("specialist_call_dedup_hit", ...)
    # Tag the active node — useful for measuring cache effectiveness
    # in optimization_report.
    attach_tag("dedup_hit")
    timer.summary(outcome="dedup_hit", ...)
    return cached
```

When the KB digest is prepended (around line 433), also tag + record digest size in `extra_json`:

```python
if not prior:
    kb_obj = getattr(app_ctx, "_specialist_kb", None)
    if isinstance(kb_obj, dict):
        kps_for_name = kb_obj.get(name, [])
        kb_digest = _format_kb_digest(kps_for_name)
        if kb_digest:
            contextual_in = f"{kb_digest}\n\n--- New question ---\n{redacted_in}"
            attach_tag("kb_digest_present")
            # Push n_kps_in_digest into the active node's extra_json
            # via a small helper (see Step 4 below).
```

Add a helper in `tools/node_trace/core.py` next to `attach_tag`:

```python
def attach_extra(**kv: Any) -> None:
    """Merge keys into the active node's extra_json. JSON-encoded at exit time."""
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    try:
        existing = json.loads(nt._extra_json) if nt._extra_json else {}
    except Exception:
        existing = {}
    existing.update(kv)
    nt._extra_json = json.dumps(existing)
```

Then in the KB-digest branch above, call `attach_extra(n_kps_in_digest=len(_active_kps(kps_for_name)))`.

- [ ] **Step 3: Run existing redacting_tool tests**

Run: `pytest tests/test_agent_factories/test_redacting_tool.py -v`
Expected: existing tests still pass (tags/extra are no-ops when no active node).

- [ ] **Step 4: Commit (offer)**

```
git add agent_factories/redacting_tool.py tools/node_trace/core.py
git commit -m "feat(node_trace): tag dedup/kb_digest on specialist + distiller nodes"
```

---

### Task 10: Wire server.py — store lifecycle + orchestrator wrapper + ttft_ms capture

**Files:**
- Modify: `server.py`
- Test: existing `tests/test_server.py` regression check + integration smoke in Task 10

- [ ] **Step 1: Construct the global store at module import**

Near the top of `server.py` (next to other module-level singletons), add:

```python
from tools.node_trace import (
    NodeTraceStore, TURN_SCOPE, TurnScope, _open_node,
)

_NODE_TRACE_STORE = NodeTraceStore(
    db_path=os.environ.get("NODE_TRACE_DB", "logs/node_traces.db"),
) if os.environ.get("NODE_TRACE_DISABLE") != "1" else None
```

- [ ] **Step 2: Pass the store onto the Session + ChatAgent + AppContext**

Find where `Session` is constructed (or `chat_agent` is built per session) and pass `node_trace_store=_NODE_TRACE_STORE`:

```python
# When constructing ChatAgent for the session:
chat_agent = ChatAgent(..., node_trace_store=_NODE_TRACE_STORE)

# When constructing AppContext for the turn (line ~1054):
ctx = AppContext(
    gateway=sess.gateway,
    case_folder=case_folder,
    logger=sess.logger,
    _specialist_kb=sess.specialist_kb,
    _distiller=getattr(orchestrator, "distiller_agent", None),
    _turn_id=turn_id,
    _emit_event=_emit_event,
    _node_trace_store=_NODE_TRACE_STORE,  # NEW
)
```

If `AppContext` is a dataclass / TypedDict, add `_node_trace_store: NodeTraceStore | None = None` to its definition in `agent_factories/app_context.py`.

- [ ] **Step 3: Set TURN_SCOPE at turn start, wrap orchestrator block**

Near the top of `_run_turn_inner` (or wherever the turn function is) — right after `turn_id` is settled — set the scope:

```python
TURN_SCOPE.set(TurnScope(
    chat_id=sess.logger.session_id,
    case_id=sess.case_id,
    turn_id=turn_id,
))
```

Then around the orchestrator block (line ~1192, `Runner.run_streamed(...)`), wrap with `_open_node` AND capture `ttft_ms` on the first stream event:

```python
from tools.node_trace import attach_latency, attach_tag

async with _open_node(_NODE_TRACE_STORE, "orchestrator", depth=0):
    attach_tag("streaming")
    streamed = Runner.run_streamed(
        orchestrator.orchestrator_agent, run_input, context=ctx,
    )
    _orch_started = time.perf_counter()
    _ttft_recorded = False
    try:
        async for event in streamed.stream_events():
            if not _ttft_recorded:
                attach_latency(
                    ttft_ms=int((time.perf_counter() - _orch_started) * 1000),
                )
                _ttft_recorded = True
            # ... existing event-dispatch logic preserved verbatim
        final_raw = streamed.final_output
        ...
```

Move the existing block under the `async with`. Indentation only — preserve all logic.

The `attach_tag("streaming")` makes streamed-vs-non-streamed observable in queries (e.g. "what's the typical TTFT for orchestrator calls"). On retry attempts (`_orch_attempt > 0`) the same NodeTrace is reused — TTFT is captured for the FIRST event of the first attempt only because `attach_latency(ttft_ms=...)` is a "first-write-wins" no-op when already set.

- [ ] **Step 4: Run server tests**

Run: `pytest tests/test_server.py -v`
Expected: existing server tests still pass.

- [ ] **Step 5: Commit (offer)**

```
git add server.py agent_factories/app_context.py
git commit -m "feat(node_trace): wire global store + per-turn scope + orchestrator wrapper"
```

---

### Task 11: tools/node_trace/turn_report.py — CLI tree reader

**Files:**
- Create: `tools/node_trace/turn_report.py`
- Test: `tests/test_tools/test_node_trace/test_turn_report.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools/test_node_trace/test_turn_report.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from tools.node_trace import NodeTraceStore
from tools.node_trace.turn_report import build_tree, render_text


def _seed_db(tmp_path: Path) -> Path:
    store = NodeTraceStore(str(tmp_path / "t.db"))
    p = store.insert(
        chat_id="c", case_id="x", turn_id="T1",
        node="specialist.spend", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    store.update(p, duration_ms=1000, outcome="ok")
    r = store.insert(
        chat_id="c", case_id="x", turn_id="T1",
        node="specialist.spend.round_1", parent_id=p, depth=1,
        started_at="2026-05-21T00:00:00.500000+00:00",
    )
    store.update(
        r,
        duration_ms=400,
        prompt_tokens=120,
        completion_tokens=8,
        total_tokens=128,
        outcome="ok",
    )
    return tmp_path / "t.db"


def test_build_tree_and_render(tmp_path: Path):
    db = _seed_db(tmp_path)
    tree = build_tree(db, chat_id="c", turn_id="T1")
    # One root, one child
    assert len(tree) == 1
    root = tree[0]
    assert root["node"] == "specialist.spend"
    assert len(root["children"]) == 1
    assert root["children"][0]["node"] == "specialist.spend.round_1"
    text = render_text(tree)
    assert "specialist.spend" in text
    assert "round_1" in text
    assert "120" in text  # prompt_tokens visible
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_tools/test_node_trace/test_turn_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.turn_report'`.

- [ ] **Step 3: Implement tools/node_trace/turn_report.py**

Create `tools/node_trace/turn_report.py`:

```python
"""CLI tree reader for the node_trace SQLite store.

Usage:
    python -m tools.node_trace.turn_report --chat <chat_id> --turn <turn_id>
    python -m tools.node_trace.turn_report --last
    python -m tools.node_trace.turn_report --last --json
    python -m tools.node_trace.turn_report --last --full-excerpts
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


_DEFAULT_DB = Path("logs/node_traces.db")


def _fetch_rows(db: Path, chat_id: str | None, turn_id: str | None) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    if chat_id is None and turn_id is None:
        last = conn.execute(
            "SELECT chat_id, turn_id FROM node_trace "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if last is None:
            return []
        chat_id, turn_id = last["chat_id"], last["turn_id"]
    where = []
    params: list[Any] = []
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    if turn_id is not None:
        where.append("turn_id = ?")
        params.append(turn_id)
    sql = "SELECT * FROM node_trace"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return rows


def build_tree(
    db: Path, *, chat_id: str | None = None, turn_id: str | None = None,
) -> list[dict]:
    """Return the rows as a nested tree (parents own children)."""
    rows = _fetch_rows(db, chat_id, turn_id)
    by_id = {r["id"]: {**r, "children": []} for r in rows}
    roots: list[dict] = []
    for r in by_id.values():
        pid = r.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(r)
        else:
            roots.append(r)
    return roots


def render_text(tree: list[dict], full_excerpts: bool = False, indent: int = 0) -> str:
    lines: list[str] = []
    for node in tree:
        prefix = "  " * indent + ("├─ " if indent > 0 else "")
        excerpt = node.get("prompt_excerpt") or ""
        if not full_excerpts and len(excerpt) > 80:
            excerpt = excerpt[:80].replace("\n", " ") + "…"
        excerpt = excerpt.replace("\n", " ")
        lines.append(
            f"{prefix}{node['node']:<46} "
            f"{(node.get('duration_ms') or 0)/1000:>6.1f}s  "
            f"in={node.get('prompt_tokens') or '-':<6} "
            f"out={node.get('completion_tokens') or '-':<5}  "
            f"{excerpt}"
        )
        if node["children"]:
            lines.append(render_text(node["children"], full_excerpts, indent + 1))
    return "\n".join(l for l in lines if l)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(_DEFAULT_DB))
    p.add_argument("--chat")
    p.add_argument("--turn")
    p.add_argument("--last", action="store_true",
                   help="latest turn across the DB (overrides --chat/--turn)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--full-excerpts", action="store_true")
    args = p.parse_args()

    chat = None if args.last else args.chat
    turn = None if args.last else args.turn
    tree = build_tree(Path(args.db), chat_id=chat, turn_id=turn)
    if args.json:
        print(json.dumps(tree, indent=2, default=str))
        return
    if not tree:
        print(f"No rows in {args.db} for chat={chat} turn={turn}.")
        return
    # Header from the first root.
    head = tree[0]
    total_in = sum((r.get("prompt_tokens") or 0) for r in _flatten(tree))
    total_out = sum((r.get("completion_tokens") or 0) for r in _flatten(tree))
    total_s = sum((r.get("duration_ms") or 0) for r in tree) / 1000
    print(
        f"chat {head['chat_id']}  turn {head['turn_id']}  "
        f"total={total_s:.1f}s  in={total_in} tok  out={total_out} tok"
    )
    print(render_text(tree, args.full_excerpts))


def _flatten(tree: list[dict]) -> list[dict]:
    out: list[dict] = []
    for n in tree:
        out.append(n)
        out.extend(_flatten(n.get("children") or []))
    return out


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_tools/test_node_trace/test_turn_report.py -v`
Expected: PASSED.

- [ ] **Step 5: Smoke-test the CLI on a fresh DB**

Run: `python -m tools.node_trace.turn_report --db /tmp/empty.db`
Expected: prints `No rows in /tmp/empty.db for chat=None turn=None.` (no crash).

- [ ] **Step 6: Commit (offer)**

```
git add tools/node_trace/turn_report.py tests/test_tools/test_node_trace/test_turn_report.py
git commit -m "feat(node_trace): add turn_report CLI tree reader"
```

---

### Task 12 (NEW): tools/node_trace/optimization_report.py — memory / tokens / latency analytics

**Files:**
- Create: `tools/node_trace/_io.py` (shared DB-read helpers)
- Create: `tools/node_trace/optimization_report.py`
- Test: `tests/test_tools/test_node_trace/test_optimization_report.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools/test_node_trace/test_optimization_report.py`:

```python
from pathlib import Path

import pytest

from tools.node_trace import NodeTraceStore
from tools.node_trace.optimization_report import (
    memory_section, tokens_section, latency_section,
)


def _seed(tmp_path: Path) -> Path:
    store = NodeTraceStore(str(tmp_path / "t.db"))
    # Synthesize one specialist with growing round prompts.
    parent = store.insert(
        chat_id="c", case_id="x", turn_id="T1",
        node="specialist.spend", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    store.update(parent, duration_ms=10_000, outcome="ok")
    for i, p_tok in enumerate([1000, 3000, 5000, 7000], start=1):
        r = store.insert(
            chat_id="c", case_id="x", turn_id="T1",
            node=f"specialist.spend.round_{i}",
            parent_id=parent, depth=1,
            started_at=f"2026-05-21T00:00:{i:02d}.000000+00:00",
        )
        store.update(
            r,
            duration_ms=2000,
            queue_wait_ms=200 if i == 4 else 10,
            llm_call_ms=1500,
            prompt_tokens=p_tok,
            completion_tokens=50,
            cached_input_tokens=200 if i > 1 else 0,
            cost_usd=p_tok * 0.15 / 1_000_000,
            outcome="ok",
        )
    return tmp_path / "t.db"


def test_memory_section_surfaces_growth(tmp_path: Path):
    db = _seed(tmp_path)
    out = memory_section(db)
    # Per-specialist context-growth-per-round table includes our specialist
    assert "specialist.spend" in out
    # Growth slope should be visible — 1000 → 7000
    assert "7000" in out or "7,000" in out


def test_tokens_section_surfaces_cache_ratio(tmp_path: Path):
    db = _seed(tmp_path)
    out = tokens_section(db)
    # Cache-hit ratio per node — at least one cached_input_tokens > 0 row
    assert "cache" in out.lower()
    # Total spend
    assert "$" in out


def test_latency_section_surfaces_queue_wait_outlier(tmp_path: Path):
    db = _seed(tmp_path)
    out = latency_section(db)
    # Round 4 had a 200ms queue_wait vs. 10ms baseline — should be visible
    assert "queue" in out.lower()
```

- [ ] **Step 2: Run the tests — expect failure**

Run: `pytest tests/test_tools/test_node_trace/test_optimization_report.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create the shared IO helpers**

Create `tools/node_trace/_io.py`:

```python
"""Shared read-side helpers for the node_trace DB. Used by both
tools/node_trace/turn_report.py and tools/node_trace/optimization_report.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def open_db(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Implement tools/node_trace/optimization_report.py**

Create `tools/node_trace/optimization_report.py`:

```python
"""Memory / tokens / latency optimization rollups over the node_trace DB.

Usage:
    python -m tools.node_trace.optimization_report memory
    python -m tools.node_trace.optimization_report tokens
    python -m tools.node_trace.optimization_report latency
    python -m tools.node_trace.optimization_report --all
    python -m tools.node_trace.optimization_report --turn <turn_id>     # scope to one turn
"""
from __future__ import annotations

import argparse
from pathlib import Path

from tools.node_trace._io import open_db


_DEFAULT_DB = Path("logs/node_traces.db")


def memory_section(db: Path, turn_id: str | None = None) -> str:
    conn = open_db(db)
    where_turn = "AND turn_id = ?" if turn_id else ""
    params = (turn_id,) if turn_id else ()
    rows = conn.execute(
        f"""
        SELECT
          substr(node, 1, instr(node || '.round_', '.round_') - 1) AS specialist,
          node,
          prompt_tokens
        FROM node_trace
        WHERE node LIKE '%.round_%' AND prompt_tokens IS NOT NULL {where_turn}
        ORDER BY specialist, started_at
        """,
        params,
    ).fetchall()
    if not rows:
        return "[memory] no per-round data yet.\n"
    # Group by specialist + show round-by-round prompt_tokens
    out = ["[memory] per-specialist context growth per round:"]
    by_spec: dict[str, list[int]] = {}
    for r in rows:
        by_spec.setdefault(r["specialist"], []).append(r["prompt_tokens"])
    for spec, series in by_spec.items():
        growth_pct = (
            int((series[-1] - series[0]) * 100 / series[0])
            if series and series[0] else 0
        )
        out.append(
            f"  {spec:<40} rounds={len(series):<3}  "
            f"start={series[0]:<6}  end={series[-1]:<6}  "
            f"growth={growth_pct:+d}%   series={series}"
        )
    return "\n".join(out) + "\n"


def tokens_section(db: Path, turn_id: str | None = None) -> str:
    conn = open_db(db)
    where_turn = "WHERE turn_id = ?" if turn_id else ""
    params = (turn_id,) if turn_id else ()
    summary = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(prompt_tokens), 0)       AS total_prompt,
          COALESCE(SUM(completion_tokens), 0)   AS total_completion,
          COALESCE(SUM(cached_input_tokens), 0) AS total_cached,
          COALESCE(SUM(reasoning_tokens), 0)    AS total_reasoning,
          COALESCE(SUM(cost_usd), 0.0)          AS total_cost
        FROM node_trace {where_turn}
        """,
        params,
    ).fetchone()
    cache_ratio = (
        summary["total_cached"] / summary["total_prompt"]
        if summary["total_prompt"] else 0.0
    )
    top = conn.execute(
        f"""
        SELECT node,
               SUM(prompt_tokens + completion_tokens) AS toks,
               SUM(cost_usd) AS dollars,
               AVG(CAST(cached_input_tokens AS REAL) /
                   NULLIF(prompt_tokens, 0)) AS cache_ratio
        FROM node_trace {where_turn}
        GROUP BY node
        ORDER BY toks DESC NULLS LAST
        LIMIT 10
        """,
        params,
    ).fetchall()
    lines = [
        "[tokens] aggregate:",
        f"  total_prompt:     {summary['total_prompt']:,}",
        f"  total_completion: {summary['total_completion']:,}",
        f"  total_cached:     {summary['total_cached']:,}  "
        f"(cache ratio: {cache_ratio:.1%})",
        f"  total_reasoning:  {summary['total_reasoning']:,}",
        f"  total_cost:       ${summary['total_cost']:.4f}",
        "",
        "[tokens] top-10 spenders by node:",
    ]
    for r in top:
        cr = r["cache_ratio"] or 0.0
        lines.append(
            f"  {r['node']:<50} toks={r['toks'] or 0:<8}  "
            f"${(r['dollars'] or 0.0):.4f}  cache={cr:.1%}"
        )
    return "\n".join(lines) + "\n"


def latency_section(db: Path, turn_id: str | None = None) -> str:
    conn = open_db(db)
    where_turn = "WHERE turn_id = ?" if turn_id else ""
    params = (turn_id,) if turn_id else ()
    rows = conn.execute(
        f"""
        SELECT node,
               AVG(duration_ms)  AS avg_total,
               AVG(queue_wait_ms) AS avg_queue,
               AVG(llm_call_ms)  AS avg_llm,
               AVG(overhead_ms)  AS avg_overhead,
               AVG(ttft_ms)      AS avg_ttft,
               COUNT(*)          AS n
        FROM node_trace {where_turn}
        GROUP BY node
        ORDER BY avg_total DESC NULLS LAST
        LIMIT 15
        """,
        params,
    ).fetchall()
    lines = [
        "[latency] top-15 slowest nodes (avg per call):",
        f"  {'node':<45} {'total':>7}  {'queue':>6}  {'llm':>6}  "
        f"{'over':>6}  {'ttft':>6}  n",
    ]
    for r in rows:
        def f(v, suffix="ms"):
            return "  -  " if v is None else f"{int(v)}{suffix}"
        lines.append(
            f"  {r['node']:<45} {f(r['avg_total']):>7}  "
            f"{f(r['avg_queue']):>6}  {f(r['avg_llm']):>6}  "
            f"{f(r['avg_overhead']):>6}  {f(r['avg_ttft']):>6}  {r['n']}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("section", nargs="?", choices=["memory", "tokens", "latency"])
    p.add_argument("--db", default=str(_DEFAULT_DB))
    p.add_argument("--turn", help="scope to one turn_id")
    p.add_argument("--all", action="store_true", help="print all three sections")
    args = p.parse_args()

    db = Path(args.db)
    if args.all or args.section is None:
        print(memory_section(db, args.turn))
        print(tokens_section(db, args.turn))
        print(latency_section(db, args.turn))
        return
    if args.section == "memory":
        print(memory_section(db, args.turn))
    elif args.section == "tokens":
        print(tokens_section(db, args.turn))
    elif args.section == "latency":
        print(latency_section(db, args.turn))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Update tools/node_trace/turn_report.py to share the IO helper**

Optional cleanup — replace `tools/node_trace/turn_report.py`'s `_fetch_rows` body with a call to `tools._node_trace_io.open_db`. Doesn't change behavior; just keeps the read path DRY.

- [ ] **Step 6: Run the tests**

Run: `pytest tests/test_tools/test_node_trace/test_optimization_report.py -v`
Expected: 3 PASSED.

- [ ] **Step 7: Smoke-test on a fresh DB**

Run: `python -m tools.node_trace.optimization_report --all --db /tmp/empty.db`
Expected: each section prints with "no data yet" messages — no crash.

- [ ] **Step 8: Commit (offer)**

```
git add tools/node_trace/_io.py tools/node_trace/optimization_report.py tests/test_tools/test_node_trace/test_optimization_report.py
git commit -m "feat(node_trace): optimization_report CLI for memory/tokens/latency"
```

---

### Task 13: Integration smoke — one end-to-end turn populates rows

**Files:**
- Modify: `tests/test_server.py` (or create `tests/test_integration/test_node_trace_smoke.py`)

- [ ] **Step 1: Locate an existing server-level test that exercises a full turn**

Run: `grep -n "async def test\|def test" tests/test_server.py | head -20`
Pick the smallest one that runs `screen → orchestrator → final` end-to-end, and clone its fixture.

- [ ] **Step 2: Write the smoke test**

Append to `tests/test_server.py` (or create `tests/test_integration/test_node_trace_smoke.py`):

```python
import os
import sqlite3
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_one_turn_populates_node_trace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NODE_TRACE_DB", str(tmp_path / "traces.db"))
    # Re-import server.py so it picks up the new env. Most server-test
    # fixtures stub out the LLM client; reuse whichever fixture in
    # tests/test_server.py runs the cheapest end-to-end turn.
    from importlib import reload
    import server
    reload(server)

    # Drive the same flow the smallest test_server.py case does.
    # ... (use the existing client/session fixture pattern)
    # After one turn:
    conn = sqlite3.connect(str(tmp_path / "traces.db"))
    nodes = [r[0] for r in conn.execute(
        "SELECT node FROM node_trace ORDER BY id"
    ).fetchall()]
    # At minimum we expect a chat.* node and an orchestrator node.
    assert any(n.startswith("chat.") for n in nodes)
    assert any(n == "orchestrator" or n.startswith("orchestrator.") for n in nodes)
```

**Note:** the exact fixture wiring depends on `test_server.py`'s helpers. If reload is awkward, an alternative is to pass an explicit `node_trace_store=NodeTraceStore(str(tmp_path / "traces.db"))` through the same constructor path the test uses. Pick whichever is less invasive to the existing test scaffolding.

- [ ] **Step 3: Run the smoke test**

Run: `pytest tests/test_server.py::test_one_turn_populates_node_trace -v`
Expected: PASSED. If `test_server.py`'s LLM stub doesn't go through `firewall_client.py` / `safechain_client.py`, the round rows won't appear — that's expected for the smoke test which only asserts the depth-0 wrappers fire.

- [ ] **Step 4: Sanity-run the whole test suite**

Run: `pytest tests/ -x --tb=short`
Expected: all green.

- [ ] **Step 5: Manual end-to-end check (offer to user)**

Suggest the user run one real server turn, then:
```
python -m tools.node_trace.turn_report --last
```
Expected: tree print with chat.* → orchestrator → specialist.* → distiller.* rows, each with duration + token counts.

- [ ] **Step 6: Commit (offer)**

```
git add tests/test_server.py
git commit -m "test(node_trace): integration smoke for end-to-end turn"
```

---

## Self-review

**Spec coverage:**
- Storage (SQLite + Langfuse-parity schema + views + WAL): Tasks 2 + 10.
- NodeTrace context manager + ACTIVE_NODE + TURN_SCOPE + attach_usage/tag/latency/io/extra: Task 3 + Task 8/9 extras.
- Pricing table → cost_usd: Task 4.
- OpenAI usage capture (incl. cached_input_tokens, reasoning_tokens, cost, llm_call_ms): Task 5.
- Safechain tiktoken capture (incl. cost, llm_call_ms): Task 6.
- Semaphore queue_wait_ms: Task 7.
- Wire sites (chat_agent, redacting_tool with cache/digest tags, server.py with TURN_SCOPE + ttft_ms): Tasks 8 + 9 + 10.
- tools/node_trace/turn_report.py CLI (tree view): Task 11.
- tools/node_trace/optimization_report.py (memory/tokens/latency analytics): Task 12.
- Env overrides (`NODE_TRACE_DB`, `NODE_TRACE_DISABLE`, `NODE_TRACE_EXCERPT_*`, `NODE_TRACE_FULL_PROMPT`, `NODE_TRACE_STORE_FULL_IO`): Tasks 2 + 3 + 10.
- Excerpt redaction: implicit — the LLM clients already redact messages before `.create()` (firewall_client.py:_redact_message, safechain_client.py:_redact_message), so excerpts captured from `messages` post-redaction inherit that pass.
- Testing: each task ships with tests; Task 13 is the smoke integration.
- Migration / rollout (additive, `ProcessTimer` untouched): no code change required; spec section is informational.

**Placeholder scan:** no TBDs / unresolved TODOs detected on a fresh re-read.

**Type consistency:** `NodeTraceStore.insert(...)` returns `int`. `NodeTrace.row_id: int`. `_open_node` returns either a `NodeTrace` or a `_NullNode` — both expose `__aenter__` / `__aexit__`, so `async with` is uniform. `attach_usage` / `attach_tag` / `attach_latency` / `attach_io` / `attach_extra` are all keyword-only and used identically across firewall_client, safechain_client, firewall_stack, redacting_tool, server. The whitelisted column set on `NodeTraceStore._ALLOWED_UPDATE_COLS` matches the `_SCHEMA` columns; new optimization columns (queue_wait_ms, llm_call_ms, ttft_ms, overhead_ms, cached_input_tokens, system_prompt_chars, reasoning_tokens, cost_usd, messages_json, output_json, tags) are present in both.

**Caveats discovered during review:**
- Tasks 8 / 9 / 10 modify files for which line numbers in the plan are approximate. The executor should re-read the surrounding 30 lines before editing each site.
- Task 13's smoke depends on `test_server.py`'s fixture shape, which I haven't read in detail. The executor should explore `test_server.py` first and adapt rather than blindly copy the placeholder structure.
- The pricing table in `tools/node_trace/pricing.py` reflects OpenAI's public pricing at plan-write time. Numbers will drift; treat the table as configuration, not code. (Spec note: move to YAML if it grows.)
- The "specialist" key in `memory_section`'s SQL uses string slicing (`substr(node, 1, instr(node || '.round_', '.round_') - 1)`) — that's a SQLite-specific way to derive the parent node name from the round-N child name. If the round suffix convention ever changes (e.g. `.r1` instead of `.round_1`), update both the writer AND this query.

---

## Execution

Plan saved. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks here using `superpowers:executing-plans`, batch with checkpoints.

The user's `.claude/memory/workflow_preferences.md` says **prefer direct Edit/Bash execution over Task-tool subagent dispatches** (subagents trigger permission prompts that create friction). So in this repo the natural choice is option 2 (inline). Offer both to the user but recommend inline given that memory.
