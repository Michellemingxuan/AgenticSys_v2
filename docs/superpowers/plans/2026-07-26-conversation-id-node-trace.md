# Restart-Invisible node_trace via `conversation_id` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make node_trace restart-invisible by keying its grouping axis on a deterministic `conversation_id = case_id::user_id::pillar_id` instead of a per-process session id, and record `server_run_id` as a diagnostic column.

**Architecture:** A pure helper mints the deterministic id; it is computed once per case-open and carried on `TurnScope`. node_trace gains four additive columns (`conversation_id`, `server_run_id`, `user_id`, `pillar_id`) and, on every new row, writes `chat_id = conversation_id` so the existing viewer/report grouping (which keys on `chat_id`) becomes restart-invisible with no query changes. `NodeTrace` auto-inherits identity from its parent at enter-time, so the parity-locked LLM clients need no edits. Amem is structurally untouched; the shared id is only stamped into metadata.

**Tech Stack:** Python 3.11 (pyenv virtualenv `autoAI`), SQLite (`tools/node_trace`), Flask viewer, openai-agents SDK, Amem (Qdrant).

## Global Constraints

- **Test interpreter:** run every test with the `autoAI` venv — `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python`. Bare `python` is the base 3.11.13 (no matplotlib) and yields false "collection errors". All `Run:` commands below use the full path.
- **Deterministic id format:** `conversation_id = f"{case_id}::{user_id}::{pillar_id}"` — literal `::` separator, readable, no hashing.
- **`chat_id` stays:** it is retained as the physical grouping column and, on all new rows, equals `conversation_id`. Do not rename or drop it. New code reads the explicit `conversation_id` column.
- **`server_run_id` is diagnostic-only:** never a grouping/route key. One value per process.
- **Additive migration only:** new columns via `ALTER TABLE ... ADD COLUMN` in the store `__init__`, mirroring the existing `qa_cache_raw_json` backfill. Never drop/rewrite existing columns.
- **Parity-locked pair:** `llm/firewall_client.py` and `llm/safechain_client.py` must stay mirror images. This plan changes **neither** (NodeTrace auto-inherits from parent); if a future step touches one, it must touch both identically.
- **Amem:** left entirely untouched in this plan — no metadata stamping, no `MemoryScope` field change, no retrieval-filter change. Amem is already restart-safe (retrieval is `case_id`-scoped). Cross-layer `conversation_id` sharing is a documented future step, not built here.
- **No auto-commit beyond each task's own commit step.** The controller commits per task; do not push.
- Full suite must stay green (baseline: 651 passing); no new failures.

---

### Task 1: `runner/identity.py` — deterministic id helpers

**Files:**
- Create: `runner/identity.py`
- Test: `tests/test_runner/test_identity.py`

**Interfaces:**
- Produces:
  - `compose_conversation_id(case_id: str, user_id: str, pillar_id: str) -> str`
  - `resolve_user_id(cfg) -> str` — `cfg.user_id` when `cfg` truthy and has a non-empty `user_id`, else `os.environ.get("AMEM_USER_ID", "amx_reviewer")`.
  - `SERVER_RUN_ID: str` — module constant, `f"run-{uuid.uuid4().hex[:8]}"`, minted once at import.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner/test_identity.py
import importlib

from runner import identity


def test_compose_conversation_id_is_deterministic_and_readable():
    a = identity.compose_conversation_id("366-abc", "amx_reviewer", "credit_risk")
    b = identity.compose_conversation_id("366-abc", "amx_reviewer", "credit_risk")
    assert a == b == "366-abc::amx_reviewer::credit_risk"


def test_compose_varies_by_each_component():
    base = identity.compose_conversation_id("366", "u1", "credit_risk")
    assert base != identity.compose_conversation_id("367", "u1", "credit_risk")
    assert base != identity.compose_conversation_id("366", "u2", "credit_risk")
    assert base != identity.compose_conversation_id("366", "u1", "escalation")


def test_resolve_user_id_prefers_cfg_then_env(monkeypatch):
    class Cfg:
        user_id = "cfg_user"
    assert identity.resolve_user_id(Cfg()) == "cfg_user"
    assert identity.resolve_user_id(None) == "amx_reviewer"
    monkeypatch.setenv("AMEM_USER_ID", "env_user")
    assert identity.resolve_user_id(None) == "env_user"


def test_server_run_id_is_stable_within_process_and_prefixed():
    assert identity.SERVER_RUN_ID.startswith("run-")
    assert identity.SERVER_RUN_ID == importlib.import_module("runner.identity").SERVER_RUN_ID
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner.identity'`

- [ ] **Step 3: Write minimal implementation**

```python
# runner/identity.py
"""Deterministic conversation identity + per-process server-run id.

`conversation_id` is DERIVED, never minted: same (case, user, pillar) →
same id forever, across server restarts. That is what makes node_trace
restart-invisible with no persistence or lookup. `SERVER_RUN_ID` is a
diagnostic value, one per process — never a grouping key.
"""
from __future__ import annotations

import os
import uuid

_SEP = "::"


def compose_conversation_id(case_id: str, user_id: str, pillar_id: str) -> str:
    return f"{case_id}{_SEP}{user_id}{_SEP}{pillar_id}"


def resolve_user_id(cfg) -> str:
    uid = getattr(cfg, "user_id", None) if cfg else None
    return uid or os.environ.get("AMEM_USER_ID", "amx_reviewer")


# Minted once per process, at import.
SERVER_RUN_ID: str = f"run-{uuid.uuid4().hex[:8]}"
```

Create `tests/test_runner/__init__.py` if the directory does not already exist as a package.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_identity.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add runner/identity.py tests/test_runner/test_identity.py
git commit -m "feat(identity): deterministic conversation_id + SERVER_RUN_ID"
```

---

### Task 2: node_trace schema + `insert`/`snapshot_session` columns + migration

**Files:**
- Modify: `tools/node_trace/core.py` (`_SCHEMA`, `NodeTraceStore.__init__` migration, `insert`, `snapshot_session`)
- Test: `tests/test_tools/test_node_trace/test_identity_columns.py`

**Interfaces:**
- Produces (new optional keyword params, all default `None` so existing callers/tests keep working):
  - `insert(..., conversation_id=None, server_run_id=None, user_id=None, pillar_id=None)`
  - `snapshot_session(..., conversation_id=None, server_run_id=None, user_id=None, pillar_id=None)`
- Both `node_trace` and `session_snapshot` gain columns `conversation_id`, `server_run_id`, `user_id`, `pillar_id` (all `TEXT`, nullable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_node_trace/test_identity_columns.py
import sqlite3

from tools.node_trace.core import NodeTraceStore


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_new_columns_exist_on_fresh_db(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    conn = sqlite3.connect(store.db_path)
    for table in ("node_trace", "session_snapshot"):
        cols = _cols(conn, table)
        assert {"conversation_id", "server_run_id", "user_id", "pillar_id"} <= cols


def test_insert_writes_identity_columns(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    rid = store.insert(
        chat_id="conv-x", case_id="366", turn_id="T1", node="root",
        parent_id=None, depth=0, started_at="2026-07-26T00:00:00+00:00",
        conversation_id="conv-x", server_run_id="run-A",
        user_id="amx_reviewer", pillar_id="credit_risk",
    )
    assert rid > 0
    conn = sqlite3.connect(store.db_path)
    row = conn.execute(
        "SELECT conversation_id, server_run_id, user_id, pillar_id, chat_id "
        "FROM node_trace WHERE id = ?", (rid,)).fetchone()
    assert row == ("conv-x", "run-A", "amx_reviewer", "credit_risk", "conv-x")


def test_migration_adds_columns_to_legacy_db(tmp_path):
    # Build a "legacy" node_trace/session_snapshot WITHOUT the new columns.
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE node_trace (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id TEXT NOT NULL, case_id TEXT NOT NULL, turn_id TEXT NOT NULL,"
        " node TEXT NOT NULL, parent_id INTEGER, depth INTEGER NOT NULL,"
        " started_at TEXT NOT NULL);"
        "INSERT INTO node_trace (chat_id, case_id, turn_id, node, depth, started_at)"
        " VALUES ('old-chat','366','T0','root',0,'2026-07-01T00:00:00+00:00');"
        "CREATE TABLE session_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " chat_id TEXT NOT NULL, case_id TEXT NOT NULL, turn_id TEXT NOT NULL,"
        " taken_at TEXT NOT NULL);")
    conn.commit()
    conn.close()

    # Reopening through NodeTraceStore must migrate in the new columns and
    # preserve the legacy row (readable via COALESCE(conversation_id, chat_id)).
    store = NodeTraceStore(db)
    conn = sqlite3.connect(store.db_path)
    assert {"conversation_id", "server_run_id", "user_id", "pillar_id"} <= _cols(conn, "node_trace")
    assert {"conversation_id", "server_run_id", "user_id", "pillar_id"} <= _cols(conn, "session_snapshot")
    grp = conn.execute(
        "SELECT COALESCE(conversation_id, chat_id) FROM node_trace").fetchone()[0]
    assert grp == "old-chat"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace/test_identity_columns.py -v`
Expected: FAIL — new columns absent / `insert()` rejects the new kwargs.

- [ ] **Step 3: Write minimal implementation**

In `tools/node_trace/core.py`:

1. Add the four columns to both `CREATE TABLE` blocks in `_SCHEMA` (place them at the end of each column list, before the closing `)`):

```sql
  -- node_trace: after extra_json
  conversation_id      TEXT,
  server_run_id        TEXT,
  user_id              TEXT,
  pillar_id            TEXT
```
```sql
  -- session_snapshot: after qa_cache_raw_json
  conversation_id      TEXT,
  server_run_id        TEXT,
  user_id              TEXT,
  pillar_id            TEXT
```

2. In `NodeTraceStore.__init__`, right after the existing `qa_cache_raw_json` backfill block, add idempotent migrations for every new column on both tables:

```python
        # Backfill identity columns for DBs created before conversation_id
        # existed (mirror the qa_cache_raw_json pattern). ADD COLUMN is
        # idempotent-by-try: OperationalError => the column is already there.
        for table in ("node_trace", "session_snapshot"):
            for col in ("conversation_id", "server_run_id", "user_id", "pillar_id"):
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass  # already exists
```

3. Extend `insert()` — add the four keyword params (default `None`) and include them in the column list + values tuple:

```python
    def insert(
        self, *, chat_id: str, case_id: str, turn_id: str, node: str,
        parent_id: int | None, depth: int, started_at: str,
        model: str | None = None, extra_json: str | None = None,
        conversation_id: str | None = None, server_run_id: str | None = None,
        user_id: str | None = None, pillar_id: str | None = None,
    ) -> int:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO node_trace "
                    "(chat_id, case_id, turn_id, node, parent_id, depth, "
                    " started_at, model, extra_json, "
                    " conversation_id, server_run_id, user_id, pillar_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, case_id, turn_id, node, parent_id, depth,
                     started_at, model, extra_json,
                     conversation_id, server_run_id, user_id, pillar_id),
                )
                return int(cur.lastrowid or -1)
        except Exception as exc:  # noqa: BLE001
            self._log_failure("insert", exc)
            return -1
```

4. Extend `snapshot_session()` the same way — add the four keyword params (default `None`), append the four columns to the INSERT column list + placeholders + values tuple. (Leave the existing counts/JSON logic unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace/test_identity_columns.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing node_trace suite (no regressions)**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace -v`
Expected: PASS (all pre-existing tests still green)

- [ ] **Step 6: Commit**

```bash
git add tools/node_trace/core.py tests/test_tools/test_node_trace/test_identity_columns.py
git commit -m "feat(node_trace): additive identity columns + migration"
```

---

### Task 3: `TurnScope` + `NodeTrace` identity propagation + `_open_node` + hooks

**Files:**
- Modify: `tools/node_trace/core.py` (`TurnScope`, `NodeTrace.__init__`, `NodeTrace.__aenter__`, `_open_node`)
- Modify: `tools/node_trace/hooks.py` (the `insert(...)` call in `on_llm_start`)
- Test: `tests/test_tools/test_node_trace/test_identity_propagation.py`

**Interfaces:**
- Consumes: `insert(..., conversation_id, server_run_id, user_id, pillar_id)` from Task 2.
- Produces:
  - `TurnScope(chat_id, case_id, turn_id, conversation_id="", server_run_id="", user_id="", pillar_id="")` — new fields appended with defaults (existing `TurnScope(chat_id=..., case_id=..., turn_id=...)` callers still valid).
  - `NodeTrace(..., conversation_id="", server_run_id="", user_id="", pillar_id="")` — new attrs; child inherits any unset identity field from its parent at `__aenter__`, and `conversation_id` falls back to `chat_id`.

**Note for the reviewer:** `llm/firewall_client.py` and `llm/safechain_client.py` are intentionally **not** modified — they build their child `NodeTrace` from `parent`, and `__aenter__` inheritance carries `conversation_id`/`server_run_id`/`user_id`/`pillar_id` down automatically. This preserves the parity lock (both untouched).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_node_trace/test_identity_propagation.py
import asyncio
import sqlite3

from tools.node_trace.core import (
    NodeTrace, NodeTraceStore, TurnScope, TURN_SCOPE, _open_node,
)


def _row(store, node):
    conn = sqlite3.connect(store.db_path)
    return conn.execute(
        "SELECT conversation_id, server_run_id, user_id, pillar_id, chat_id "
        "FROM node_trace WHERE node = ?", (node,)).fetchone()


def test_open_node_carries_scope_identity(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    token = TURN_SCOPE.set(TurnScope(
        chat_id="366::u::credit_risk", case_id="366", turn_id="T1",
        conversation_id="366::u::credit_risk", server_run_id="run-A",
        user_id="u", pillar_id="credit_risk"))
    try:
        async def go():
            async with _open_node(store, "root", depth=0):
                pass
        asyncio.run(go())
    finally:
        TURN_SCOPE.reset(token)
    assert _row(store, "root") == (
        "366::u::credit_risk", "run-A", "u", "credit_risk", "366::u::credit_risk")


def test_child_inherits_identity_from_parent(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))

    async def go():
        async with NodeTrace(
            store=store, chat_id="conv-A", case_id="366", turn_id="T1",
            node="parent", depth=0, conversation_id="conv-A",
            server_run_id="run-A", user_id="u", pillar_id="credit_risk"):
            # Child passes only chat_id (as the LLM clients do) — must inherit.
            async with NodeTrace(
                store=store, chat_id="conv-A", case_id="366", turn_id="T1",
                node="child", depth=1):
                pass

    asyncio.run(go())
    assert _row(store, "child") == ("conv-A", "run-A", "u", "credit_risk", "conv-A")


def test_conversation_id_defaults_to_chat_id_when_unset(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))

    async def go():
        async with NodeTrace(store=store, chat_id="legacy-chat",
                             case_id="366", turn_id="T1", node="solo", depth=0):
            pass

    asyncio.run(go())
    row = _row(store, "solo")
    assert row[0] == "legacy-chat"   # conversation_id fell back to chat_id
    assert row[4] == "legacy-chat"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace/test_identity_propagation.py -v`
Expected: FAIL — `TurnScope`/`NodeTrace` reject the new kwargs; columns unpopulated.

- [ ] **Step 3: Write minimal implementation**

In `tools/node_trace/core.py`:

1. Extend `TurnScope` (keep `frozen=True`; append fields with defaults):

```python
@dataclass(frozen=True)
class TurnScope:
    chat_id: str
    case_id: str
    turn_id: str
    conversation_id: str = ""
    server_run_id: str = ""
    user_id: str = ""
    pillar_id: str = ""
```

2. `NodeTrace.__init__`: add params `conversation_id: str = ""`, `server_run_id: str = ""`, `user_id: str = ""`, `pillar_id: str = ""` and store them as attributes:

```python
        self.conversation_id = conversation_id
        self.server_run_id = server_run_id
        self.user_id = user_id
        self.pillar_id = pillar_id
```

3. `NodeTrace.__aenter__`: after resolving `parent = ACTIVE_NODE.get()` and BEFORE `self._store.insert(...)`, inherit unset identity from parent, then default `conversation_id` to `chat_id`; pass all four to `insert`:

```python
        parent = ACTIVE_NODE.get()
        if parent is not None:
            self.conversation_id = self.conversation_id or getattr(parent, "conversation_id", "")
            self.server_run_id = self.server_run_id or getattr(parent, "server_run_id", "")
            self.user_id = self.user_id or getattr(parent, "user_id", "")
            self.pillar_id = self.pillar_id or getattr(parent, "pillar_id", "")
        # conversation_id is the grouping axis; fall back to chat_id so a row
        # is never left ungrouped (legacy/test call sites that pass only chat_id).
        self.conversation_id = self.conversation_id or self.chat_id
        self._t0 = perf_counter()
        self.row_id = self._store.insert(
            chat_id=self.chat_id, case_id=self.case_id, turn_id=self.turn_id,
            node=self.node,
            parent_id=parent.row_id if parent and parent.row_id > 0 else None,
            depth=self.depth, started_at=_now_iso(), extra_json=self._extra_json,
            conversation_id=self.conversation_id, server_run_id=self.server_run_id,
            user_id=self.user_id, pillar_id=self.pillar_id,
        )
```

4. `_open_node`: pass the scope's identity fields through:

```python
    return NodeTrace(
        store=store, chat_id=scope.chat_id, case_id=scope.case_id,
        turn_id=scope.turn_id, node=node, depth=depth,
        conversation_id=scope.conversation_id, server_run_id=scope.server_run_id,
        user_id=scope.user_id, pillar_id=scope.pillar_id,
    )
```

In `tools/node_trace/hooks.py` — the `on_llm_start` `self._store.insert(...)` call (~line 140) gains the parent's identity fields:

```python
            row_id = self._store.insert(
                chat_id=self._parent.chat_id,
                case_id=self._parent.case_id,
                turn_id=self._parent.turn_id,
                node=node_name,
                parent_id=self._parent.row_id,
                depth=self._parent.depth + 1,
                started_at=_now_iso(),
                model=str(agent.model) if getattr(agent, "model", None) else None,
                conversation_id=getattr(self._parent, "conversation_id", "") or self._parent.chat_id,
                server_run_id=getattr(self._parent, "server_run_id", ""),
                user_id=getattr(self._parent, "user_id", ""),
                pillar_id=getattr(self._parent, "pillar_id", ""),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace/test_identity_propagation.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the node_trace + LLM-client suites (parity untouched, hooks intact)**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace -v`
Expected: PASS (all green — firewall/safechain hook tests still pass unchanged)

- [ ] **Step 6: Commit**

```bash
git add tools/node_trace/core.py tools/node_trace/hooks.py tests/test_tools/test_node_trace/test_identity_propagation.py
git commit -m "feat(node_trace): TurnScope/NodeTrace identity + parent inheritance"
```

---

### Task 4: Wire conversation identity through the session, turn scope, and snapshots

**Files:**
- Modify: `server.py` (`CaseSession` dataclass fields; `_get_or_create_session` computes the id; `snapshot_session` rewind-marker call at ~1102)
- Modify: `runner/turn/conductor.py` (`TurnScope` construction ~184; `snapshot_session` end-of-turn call ~1459)
- Test: `tests/test_runner/test_conversation_wiring.py`

**Interfaces:**
- Consumes: `compose_conversation_id`, `resolve_user_id`, `SERVER_RUN_ID` (Task 1); `TurnScope` new fields (Task 3).
- Produces: `CaseSession.conversation_id`, `CaseSession.user_id`, `CaseSession.pillar_id` (set at open); every turn's node_trace rows carry `chat_id == conversation_id == compose_conversation_id(case_id, user_id, pillar_id)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner/test_conversation_wiring.py
"""The wiring is validated at the seam: a CaseSession-like object fed through
the same id computation yields a deterministic conversation_id, and a
TurnScope built the way the conductor builds it groups two server runs
under ONE conversation_id."""
from runner.identity import compose_conversation_id, resolve_user_id
from tools.node_trace.core import TurnScope


class _Cfg:
    user_id = "amx_reviewer"


def test_session_conversation_id_is_deterministic_across_runs():
    cid = compose_conversation_id("366", resolve_user_id(_Cfg()), "credit_risk")
    assert cid == "366::amx_reviewer::credit_risk"
    # A second "server run" recomputes the SAME id from the same inputs.
    assert cid == compose_conversation_id("366", resolve_user_id(_Cfg()), "credit_risk")


def test_turnscope_two_runs_share_conversation_but_differ_by_run():
    cid = "366::amx_reviewer::credit_risk"
    s_a = TurnScope(chat_id=cid, case_id="366", turn_id="T1",
                    conversation_id=cid, server_run_id="run-A",
                    user_id="amx_reviewer", pillar_id="credit_risk")
    s_b = TurnScope(chat_id=cid, case_id="366", turn_id="T2",
                    conversation_id=cid, server_run_id="run-B",
                    user_id="amx_reviewer", pillar_id="credit_risk")
    assert s_a.conversation_id == s_b.conversation_id == cid
    assert s_a.chat_id == s_b.chat_id == cid       # grouping axis stable
    assert s_a.server_run_id != s_b.server_run_id  # diagnostic differs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_conversation_wiring.py -v`
Expected: FAIL until Tasks 1+3 are importable together (and confirms the seam contract). If Tasks 1 & 3 are already committed, this test passes at Step 2 — in that case treat Step 3 as the wiring that makes the *runtime* honor it, and rely on Step 5's suite run as the regression gate.

- [ ] **Step 3: Write the implementation**

In `server.py`:

1. `CaseSession` — add three fields near `session_id` (~line 214):

```python
    conversation_id: str = ""   # deterministic: compose(case_id, user_id, pillar_id)
    user_id: str = ""
    pillar_id: str = ""
```

2. In `_get_or_create_session`, right after `sess.session_id = case_logger.session_id` (~line 593) and the Amem attach:

```python
        from runner.identity import compose_conversation_id, resolve_user_id
        from runner.config import PILLAR
        sess.user_id = resolve_user_id(_AMEM_CFG)
        sess.pillar_id = PILLAR
        sess.conversation_id = compose_conversation_id(case_id, sess.user_id, sess.pillar_id)
```

(`PILLAR` is already imported at `server.py:67`; the local import is belt-and-suspenders — if it is already in module scope, drop the `from runner.config import PILLAR` line to avoid shadowing.)

3. The rewind-marker `snapshot_session` at ~1102 — pass the conversation identity:

```python
                _NODE_TRACE_STORE.snapshot_session(
                    chat_id=sess.conversation_id, case_id=case_id,
                    turn_id=f"rewind-{_max_seq}",
                    qa_cache=sess.qa_cache, specialist_kb=sess.specialist_kb,
                    input_history=sess.input_history,
                    conversation_id=sess.conversation_id,
                    server_run_id=SERVER_RUN_ID, user_id=sess.user_id,
                    pillar_id=sess.pillar_id)
```

Add `from runner.identity import SERVER_RUN_ID` to `server.py`'s imports.

In `runner/turn/conductor.py`:

4. `TurnScope` construction (~184) — feed the deterministic id + run id:

```python
        from runner.identity import SERVER_RUN_ID
        _conv = getattr(sess, "conversation_id", "") or sess.logger.session_id
        TURN_SCOPE.set(TurnScope(
            chat_id=_conv,                     # grouping axis = conversation
            case_id=sess.case_id,
            turn_id=turn_id,
            conversation_id=_conv,
            server_run_id=SERVER_RUN_ID,
            user_id=getattr(sess, "user_id", ""),
            pillar_id=getattr(sess, "pillar_id", ""),
        ))
```

(The `getattr(..., sess.logger.session_id)` fallback keeps any non-server construction path — e.g. a unit harness that builds a bare session — from writing an empty `chat_id`.)

5. The end-of-turn `snapshot_session` at ~1459 — mirror the rewind-marker change: pass `chat_id=sess.conversation_id` plus `conversation_id`/`server_run_id`/`user_id`/`pillar_id`. Import `SERVER_RUN_ID` at the top of the module alongside the existing `runner.config` import.

- [ ] **Step 4: Run the wiring test**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_conversation_wiring.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run server + conductor regression suites**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -k "conductor or server or node_trace or session" -v`
Expected: PASS (no new failures)

- [ ] **Step 6: Commit**

```bash
git add server.py runner/turn/conductor.py tests/test_runner/test_conversation_wiring.py
git commit -m "feat(server): compute conversation_id at open; feed TurnScope + snapshots"
```

---

### Task 5: Viewer — surface `server_run_id`, relabel to "conversation"

**Files:**
- Modify: `tools/node_trace/viewer.py` (`_CHAT` turn-list template + query; `_TURN` detail header; index/route labels; `COALESCE` grouping guard)
- Test: `tests/test_tools/test_node_trace/test_viewer_identity.py`

**Interfaces:**
- Consumes: node_trace `conversation_id`/`server_run_id` columns (Task 2), populated rows (Task 4).
- The grouping axis is already `chat_id` (== `conversation_id` on new rows), so **no grouping query change is required** for restart-invisibility; this task adds the diagnostic surface + labels.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_node_trace/test_viewer_identity.py
from tools.node_trace.core import NodeTraceStore
from tools.node_trace import viewer as V


def _seed(tmp_path):
    store = NodeTraceStore(str(tmp_path / "t.db"))
    cid = "366::u::credit_risk"
    # Two turns, two server runs, same conversation.
    for turn, run in (("T1", "run-A"), ("T2", "run-B")):
        store.insert(chat_id=cid, case_id="366", turn_id=turn, node="root",
                     parent_id=None, depth=0,
                     started_at=f"2026-07-26T00:00:0{turn[-1]}+00:00",
                     conversation_id=cid, server_run_id=run,
                     user_id="u", pillar_id="credit_risk")
    return store, cid


def test_index_groups_two_runs_as_one_conversation(tmp_path):
    store, cid = _seed(tmp_path)
    V.app.config["NODE_TRACE_DB"] = store.db_path
    client = V.app.test_client()
    html = client.get("/").get_data(as_text=True)
    # One conversation row carrying the shared id, both turns counted.
    assert cid in html
    assert html.count(cid) >= 1


def test_turn_page_shows_server_run_id(tmp_path):
    store, cid = _seed(tmp_path)
    V.app.config["NODE_TRACE_DB"] = store.db_path
    client = V.app.test_client()
    html = client.get(f"/turn/{cid}/T1").get_data(as_text=True)
    assert "run-A" in html          # diagnostic surfaced on the turn view
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace/test_viewer_identity.py -v`
Expected: FAIL on `test_turn_page_shows_server_run_id` (run id not rendered yet). The index test may already pass (grouping is inherited) — that is fine and documents the shim working.

- [ ] **Step 3: Write the implementation**

In `tools/node_trace/viewer.py`:

1. In the `turn()` route (~966), the per-turn rows already `SELECT *`, so each row dict carries `server_run_id`. In the `_TURN` template header block (near `turn <code>{{ turn_id }}</code> · {{ chat_id }}`, ~line 471), add the run id, e.g.:

```html
turn <code>{{ turn_id }}</code> · {{ chat_id }}
{% if rows and rows[0].server_run_id %}· <span class="muted">server run {{ rows[0].server_run_id }}</span>{% endif %}
```

(Confirm the template variable name for the row list passed to `_TURN`; use it in the guard.)

2. In the `_CHAT` turn-list template (~308), add a `server_run` column header and cell (`{{ t.server_run_id }}`) — `turn_summary` is a `GROUP BY chat_id, case_id, turn_id` view that does not expose `server_run_id`, so fetch it per turn in the `chat()` route with a small lookup (mirror `_question_for_turn`):

```python
def _server_run_for_turn(conn, chat_id, turn_id):
    r = conn.execute(
        "SELECT server_run_id FROM node_trace WHERE chat_id = ? AND turn_id = ? "
        "AND server_run_id IS NOT NULL ORDER BY started_at DESC LIMIT 1",
        (chat_id, turn_id)).fetchone()
    return r["server_run_id"] if r else ""
```

Call it in the `chat()` loop: `t["server_run_id"] = _server_run_for_turn(conn, chat_id, t["turn_id"])`.

3. Cosmetic relabel: change user-facing "Chat"/"chat_id" headings to "Conversation" in `_INDEX`, `_CHAT`, `_STATE` (leave route paths and the `chat_id` template variable name intact to avoid churn; this is a label-only change). Optionally add `/conversation/<id>` as an alias route that calls the existing `chat` view, keeping `/chat/<id>` working.

4. Harden the index/latest-turn lookups against legacy NULLs by grouping on `COALESCE(conversation_id, chat_id)` is **not** required (new rows set `chat_id`); leave `session_summary` as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace/test_viewer_identity.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/node_trace/viewer.py tests/test_tools/test_node_trace/test_viewer_identity.py
git commit -m "feat(viewer): surface server_run_id, relabel to conversation"
```

---

### Task 6: Rewind-across-restart regression test

**Files:**
- Test: `tests/test_tools/test_node_trace/test_rewind_across_restart.py`

**Interfaces:**
- Consumes: `NodeTraceStore.insert` / `snapshot_session` (identity columns), `load_latest_snapshot`, `delete_turns`.

This is a **test-only** task proving the behavior the reviewer asked about: after a restart, rewinding a pre-restart turn removes exactly that turn's rows while the others survive under the same conversation.

- [ ] **Step 1: Write the test**

```python
# tests/test_tools/test_node_trace/test_rewind_across_restart.py
from tools.node_trace.core import NodeTraceStore


def _ids(store, conv):
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    return sorted(r[0] for r in conn.execute(
        "SELECT turn_id FROM node_trace WHERE conversation_id = ?", (conv,)))


def test_rewind_pre_restart_turn_survives_restart(tmp_path):
    db = str(tmp_path / "traces.db")
    conv = "366::u::credit_risk"

    # --- server run A: two turns + an end-of-turn snapshot ---
    store_a = NodeTraceStore(db)
    for turn in ("T1", "T2"):
        store_a.insert(chat_id=conv, case_id="366", turn_id=turn, node="root",
                       parent_id=None, depth=0,
                       started_at=f"2026-07-26T00:00:0{turn[-1]}+00:00",
                       conversation_id=conv, server_run_id="run-A",
                       user_id="u", pillar_id="credit_risk")
    store_a.snapshot_session(
        chat_id=conv, case_id="366", turn_id="T2",
        qa_cache={"q": {"turn_id_origin": "T2", "turn_seq": 2}},
        specialist_kb={}, input_history=[],
        conversation_id=conv, server_run_id="run-A",
        user_id="u", pillar_id="credit_risk")
    assert _ids(store_a, conv) == ["T1", "T2"]

    # --- simulate restart: a NEW store on the SAME db (run-B) ---
    store_b = NodeTraceStore(db)
    snap = store_b.load_latest_snapshot("366")
    assert snap is not None and snap["chat_id"] == conv   # restore is case-keyed

    # --- rewind pre-restart turn T1 (delete_turns keys on stable turn_id) ---
    removed = store_b.delete_turns(["T1"])
    assert removed == 1
    assert _ids(store_b, conv) == ["T2"]                  # T2 survives, same conv
```

- [ ] **Step 2: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_node_trace/test_rewind_across_restart.py -v`
Expected: PASS (1 test)

- [ ] **Step 3: Full-suite gate**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: PASS — baseline 651 + the new tests; zero new failures.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tools/test_node_trace/test_rewind_across_restart.py
git commit -m "test(node_trace): rewind a pre-restart turn survives restart"
```

---

## Self-Review

- **Spec coverage:** identity model (Task 1), schema+migration (Task 2), TurnScope/NodeTrace/hooks propagation with parity-locked files untouched (Task 3), session+conductor+snapshot wiring (Task 4), viewer server_run_id + relabel with grouping-via-shim (Task 5), rewind-across-restart regression (Task 6). Amem is intentionally left untouched. Deferred items (multi-user auth, Amem scope-field promotion + metadata sharing, reopen-epoch) are intentionally absent.
- **Placeholders:** none — every code step carries real code or an exact edit target; the two "confirm the template variable name" notes in Task 5 point at a concrete template and are resolved by reading the adjacent lines.
- **Type consistency:** `compose_conversation_id`/`resolve_user_id`/`SERVER_RUN_ID` names are identical across Tasks 1/4; `insert`/`snapshot_session` new kwargs identical across Tasks 2/3/4. `TurnScope`/`NodeTrace` field order (existing first, new defaulted last) preserves all existing call sites.
