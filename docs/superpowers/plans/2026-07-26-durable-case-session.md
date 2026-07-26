# Durable Case Session + Per-Case Clear History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a case's single session durable across server restarts — reopening a case restores its conversation memory and thread so the user never perceives a restart — and scope "Clear History" to the active case (purging that case's memory), with "Rewind" removing the undone turns' memory.

**Architecture:** The server already snapshots each turn's cross-turn RAM state to SQLite (`session_snapshot`) and holds durable knowledge in Amem/Qdrant. We (1) persist the *raw* `qa_cache` in the snapshot (today only a projection is stored), (2) restore `qa_cache`/`specialist_kb`/`input_history` on case open, (3) serve the thread from a new `GET /history` endpoint, (4) purge the case's Amem memory on Clear History and re-snapshot on partial rewind, and (5) update the frontend to fetch history on case open and clear per-case.

**Tech Stack:** Python 3.11 (`autoAI` venv), SQLite (`NodeTraceStore`), Flask, Amem (`memory/` helpers), pytest; React 19 + TypeScript + Zustand + Vite + Vitest (`CaseReviewChat`).

## Global Constraints

- **Python tests** run under `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest <path> -v`. Never bare `python`. Amem is editable-installed; Qdrant not required for these tests (use temp SQLite + fake Amem).
- **Frontend tests** run in `CaseReviewChat/` via `npx vitest run <path>` (vitest config is inline in `vite.config.ts`: `environment: 'jsdom'`, `globals: true`).
- `NodeTraceStore` owns one long-lived `self._conn` (`sqlite3.connect(..., isolation_level=None, check_same_thread=False)`, WAL), a `threading.Lock` (`self._lock`) for **writes only** (reads don't lock), and swallows all exceptions via `self._log_failure(op, exc)`. `self._conn` has **no** `row_factory` — SELECT explicit columns and index the tuple.
- `_NODE_TRACE_STORE` (server.py global from `runner.config`) **can be `None`** — guard every use with `if _NODE_TRACE_STORE is not None:`.
- All new Amem/store calls must be defensive: a failure logs and never breaks a turn, a rewind, or session creation.
- `snapshot_session` runs in the hot `_finalize` path — the added raw-qa_cache dump must stay cheap (one `json.dumps` of a dict it already holds).
- Message dedup: `appendMessage` dedups by `msg.id`; history messages must merge idempotently with SSE (dedup by `(turn_id, role)` for agent messages).
- NEVER commit/push unless the human asks in the current turn.

---

## File Structure

**Backend (`AgenticSys_v2`):**
- `tools/node_trace/core.py` — `session_snapshot` gains `qa_cache_raw_json`; `snapshot_session` writes it; new read method `load_latest_snapshot(case_id)`.
- `memory/rewind.py` — new `delete_case_memory(amem, cfg, case_id)`; `memory/__init__.py` re-export.
- `server.py` — `_restore_session_state(sess, case_id)` + call it in `_get_or_create_session`; `delete_case_memory` on full rewind; re-snapshot on partial rewind; `GET /api/cases/<case_id>/history`.
- `tests/memory/`, `tests/test_tools/` — new tests.

**Frontend (`CaseReviewChat`):**
- `src/api.ts` — `fetchHistory(caseId)`.
- `src/types.ts` — `HistoryResponse` type; `clearCaseHistory` / `setCaseHistory` action sigs.
- `src/store.ts` — `clearCaseHistory`, `setCaseHistory`; `appendMessage` `(turn_id, role)` dedup.
- `src/components/Sidebar/Sidebar.tsx` — per-case clear.
- `src/hooks/useCaseHistory.ts` (new) — fetch history on `activeCase` change; used by `ChatPanel`.
- `src/__tests__/` — new vitest specs.

---

## Task 1: Persist raw qa_cache + `load_latest_snapshot`

**Files:**
- Modify: `tools/node_trace/core.py` (schema `_SCHEMA` ~line 63-78; `__init__` migration ~line 137; `snapshot_session` ~line 209-268)
- Test: `tests/test_tools/test_session_snapshot_restore.py`

**Interfaces:**
- Produces: `session_snapshot.qa_cache_raw_json TEXT`; `snapshot_session` unchanged signature but also writes it; `NodeTraceStore.load_latest_snapshot(case_id: str) -> dict | None` returning `{"qa_cache": dict, "specialist_kb": dict, "input_history": list, "chat_id": str}` or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_session_snapshot_restore.py
import os, tempfile
from tools.node_trace.core import NodeTraceStore


def _store():
    d = tempfile.mkdtemp()
    return NodeTraceStore(os.path.join(d, "t.db"))


def test_snapshot_persists_raw_qa_cache_and_loads_latest():
    s = _store()
    qa = {"why held?": {"answer": "FICO.", "turn_id_origin": "t1",
                        "origin_question": "Why held?", "turn_seq": 1}}
    kb = {"risk": [{"topic": "fico", "claim": "low", "captured_at_turn": "t1"}]}
    ih = [{"role": "user", "content": "Why held?"}]
    s.snapshot_session(chat_id="c-1", case_id="CASE", turn_id="t1",
                       qa_cache=qa, specialist_kb=kb, input_history=ih)
    snap = s.load_latest_snapshot("CASE")
    assert snap is not None
    assert snap["qa_cache"] == qa          # RAW dict, not the episodic projection
    assert snap["specialist_kb"] == kb
    assert snap["input_history"] == ih
    assert snap["chat_id"] == "c-1"


def test_load_latest_returns_most_recent():
    s = _store()
    s.snapshot_session(chat_id="c-1", case_id="CASE", turn_id="t1",
                       qa_cache={"a": {"turn_seq": 1}}, specialist_kb={}, input_history=[])
    s.snapshot_session(chat_id="c-1", case_id="CASE", turn_id="t2",
                       qa_cache={"a": {"turn_seq": 1}, "b": {"turn_seq": 2}},
                       specialist_kb={}, input_history=[])
    snap = s.load_latest_snapshot("CASE")
    assert set(snap["qa_cache"].keys()) == {"a", "b"}   # latest


def test_load_latest_none_when_absent():
    assert _store().load_latest_snapshot("NOPE") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_session_snapshot_restore.py -v`
Expected: FAIL — `AttributeError: 'NodeTraceStore' object has no attribute 'load_latest_snapshot'` (and/or the raw column missing).

- [ ] **Step 3: Add the column to `_SCHEMA`**

In `tools/node_trace/core.py`, in the `session_snapshot` CREATE TABLE (the `_SCHEMA` string, after `input_history_json  TEXT`), add a column:

```sql
    input_history_json  TEXT,
    qa_cache_raw_json   TEXT
```

- [ ] **Step 4: Add an idempotent migration in `__init__`**

In `NodeTraceStore.__init__`, immediately after `self._conn.executescript(_SCHEMA)` succeeds (after the try/except around line 137), add a guarded ALTER for existing DBs that predate the column:

```python
        # Backfill column for DBs created before qa_cache_raw_json existed.
        try:
            self._conn.execute(
                "ALTER TABLE session_snapshot ADD COLUMN qa_cache_raw_json TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
```

- [ ] **Step 5: Write the raw dict in `snapshot_session`**

In `snapshot_session`, add the raw dump next to the existing `qa_json`/`kb_json`/`ih_json` (keep `qa_json` as the projection). After the line `ih_json = json.dumps(input_history, default=str) if input_history else None` add:

```python
            qa_raw_json = json.dumps(qa_cache, default=str) if qa_cache else None
```

Then extend the INSERT column list, placeholders, and values tuple to include `qa_cache_raw_json`:

```python
                    "INSERT INTO session_snapshot "
                    "(chat_id, case_id, turn_id, taken_at, "
                    " qa_cache_n, kb_specialists_n, kb_kps_n, "
                    " input_history_items, input_history_chars, "
                    " qa_cache_json, specialist_kb_json, input_history_json, "
                    " qa_cache_raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, case_id, turn_id, _now_iso(),
                     qa_n, kb_specialists_n, kb_kps_n,
                     ih_items, ih_chars,
                     qa_json, kb_json, ih_json,
                     qa_raw_json),
```

- [ ] **Step 6: Add `load_latest_snapshot` (first read method — no lock)**

Add to the `NodeTraceStore` class (e.g. after `snapshot_session`):

```python
    def load_latest_snapshot(self, case_id: str) -> dict | None:
        """Return the most recent snapshot for a case as restorable state, or
        None. Read-only (no lock). Decodes each JSON column defensively."""
        try:
            row = self._conn.execute(
                "SELECT chat_id, qa_cache_raw_json, specialist_kb_json, "
                "input_history_json FROM session_snapshot "
                "WHERE case_id = ? ORDER BY taken_at DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            if row is None:
                return None
            chat_id, qa_raw, kb_json, ih_json = row

            def _load(blob, default):
                if not blob:
                    return default
                try:
                    return json.loads(blob)
                except Exception:  # noqa: BLE001
                    return default

            return {
                "chat_id": chat_id,
                "qa_cache": _load(qa_raw, {}),
                "specialist_kb": _load(kb_json, {}),
                "input_history": _load(ih_json, []),
            }
        except Exception as exc:  # noqa: BLE001
            self._log_failure("load_latest_snapshot", exc)
            return None
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_session_snapshot_restore.py -v`
Expected: PASS (3 passed).

- [ ] **Step 8: Regression — existing node_trace tests still pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -k "node_trace or snapshot or trace" -v`
Expected: PASS (no regressions from the schema/insert change).

- [ ] **Step 9: Commit**

```bash
git add tools/node_trace/core.py tests/test_tools/test_session_snapshot_restore.py
git commit -m "feat(session): persist raw qa_cache + load_latest_snapshot for restore"
```

---

## Task 2: `delete_case_memory` — whole-case Amem purge

**Files:**
- Modify: `memory/rewind.py`; `memory/__init__.py` (re-export)
- Test: `tests/memory/test_delete_case_memory.py`

**Interfaces:**
- Consumes: `AmemConfig`, `build_scope` (existing); `FakeAmem` (`tests/memory/_fake_amem.py`).
- Produces: `delete_case_memory(amem, cfg, *, case_id: str) -> int` (sync) — lists ALL records for the case (all levels, working included) and deletes each; returns count; never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_delete_case_memory.py
from memory.config import AmemConfig
from memory.rewind import delete_case_memory
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_delete_case_memory_deletes_all_listed():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="a", content="x"), FakeRecord(id="b", content="y")]
    n = delete_case_memory(fake, CFG, case_id="c1")
    assert n == 2
    assert set(fake.deleted) == {"a", "b"}


def test_delete_case_memory_scope_is_case_only():
    fake = FakeAmem()
    delete_case_memory(fake, CFG, case_id="c1")
    # list_memories called with a case-only scope + include_working
    assert fake.list_calls and fake.list_calls[-1]["scope"].case_id == "c1"
    assert fake.list_calls[-1]["scope"].turn_id is None
    assert fake.list_calls[-1].get("include_working") is True


def test_delete_case_memory_survives_errors():
    class Boom(FakeAmem):
        def list_memories(self, **k):
            raise RuntimeError("down")
    assert delete_case_memory(Boom(), CFG, case_id="c1") == 0
```

(Note: if `FakeAmem` doesn't record `list_calls`, add a `self.list_calls = []` list and append `kwargs` at the top of its `list_memories` — a one-line test-double enhancement.)

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_delete_case_memory.py -v`
Expected: FAIL — `ImportError: cannot import name 'delete_case_memory'`.

- [ ] **Step 3: Implement `delete_case_memory`**

Append to `memory/rewind.py`:

```python
def delete_case_memory(amem, cfg: AmemConfig, *, case_id: str) -> int:
    """Purge ALL Amem memory for a case (working/conversation/case, every turn).
    Used by Clear History. Never raises."""
    deleted = 0
    try:
        records = amem.list_memories(
            scope=build_scope(cfg, case_id),   # case-only: no turn/agent filter
            include_working=True,
        )
    except Exception:
        return 0
    for rec in records:
        try:
            if amem.delete_memory(rec.id):
                deleted += 1
        except Exception:
            continue
    return deleted
```

- [ ] **Step 4: Re-export**

In `memory/__init__.py`, add `delete_case_memory` to the `from .rewind import ...` line and to `__all__`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_delete_case_memory.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add memory/rewind.py memory/__init__.py tests/memory/_fake_amem.py tests/memory/test_delete_case_memory.py
git commit -m "feat(memory): delete_case_memory whole-case Amem purge"
```

---

## Task 3: Restore session memory on case open

**Files:**
- Modify: `server.py` (`_get_or_create_session` ~line 551-571; add `_restore_session_state` helper)
- Test: `tests/memory/test_session_restore.py`

**Interfaces:**
- Consumes: `NodeTraceStore.load_latest_snapshot` (Task 1).
- Produces: `_restore_session_state(sess, case_id)` — module-level helper in `server.py` that, when `_NODE_TRACE_STORE` has a snapshot for the case, sets `sess.qa_cache`, `sess.specialist_kb`, `sess.input_history`, `sess._qa_turn_seq`; no-op otherwise.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_session_restore.py
import types
import server


class _FakeStore:
    def __init__(self, snap):
        self._snap = snap
    def load_latest_snapshot(self, case_id):
        return self._snap


def _sess():
    return types.SimpleNamespace(qa_cache={}, specialist_kb={},
                                 input_history=[], _qa_turn_seq=0,
                                 logger=types.SimpleNamespace(log=lambda *a, **k: None))


def test_restore_populates_ram_from_snapshot(monkeypatch):
    snap = {"chat_id": "c-1",
            "qa_cache": {"q1": {"turn_seq": 1}, "q2": {"turn_seq": 2}},
            "specialist_kb": {"risk": [{"topic": "fico"}]},
            "input_history": [{"role": "user", "content": "hi"}]}
    monkeypatch.setattr(server, "_NODE_TRACE_STORE", _FakeStore(snap))
    sess = _sess()
    server._restore_session_state(sess, "CASE")
    assert set(sess.qa_cache) == {"q1", "q2"}
    assert sess.specialist_kb == {"risk": [{"topic": "fico"}]}
    assert sess.input_history == [{"role": "user", "content": "hi"}]
    assert sess._qa_turn_seq == 2          # continues from max turn_seq


def test_restore_noop_without_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_NODE_TRACE_STORE", _FakeStore(None))
    sess = _sess()
    server._restore_session_state(sess, "CASE")
    assert sess.qa_cache == {} and sess._qa_turn_seq == 0


def test_restore_noop_without_store(monkeypatch):
    monkeypatch.setattr(server, "_NODE_TRACE_STORE", None)
    sess = _sess()
    server._restore_session_state(sess, "CASE")   # must not raise
    assert sess.qa_cache == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_session_restore.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_restore_session_state'`.

- [ ] **Step 3: Add the helper**

In `server.py` (module level, near `_get_or_create_session`), add:

```python
def _restore_session_state(sess, case_id: str) -> None:
    """Restore a case's cross-turn RAM (qa_cache, specialist_kb, input_history)
    from the latest durable snapshot, so a server restart is invisible.
    Best-effort; never raises."""
    if _NODE_TRACE_STORE is None:
        return
    try:
        snap = _NODE_TRACE_STORE.load_latest_snapshot(case_id)
    except Exception:
        snap = None
    if not snap:
        return
    try:
        sess.qa_cache = snap.get("qa_cache") or {}
        sess.specialist_kb = snap.get("specialist_kb") or {}
        sess.input_history = snap.get("input_history") or []
        sess._qa_turn_seq = max(
            (e.get("turn_seq", 0) for e in sess.qa_cache.values()
             if isinstance(e, dict)), default=0)
        sess.logger.log("session_restored", {
            "case_id": case_id,
            "qa_entries": len(sess.qa_cache),
            "kb_specialists": len(sess.specialist_kb)})
    except Exception:
        pass
```

- [ ] **Step 4: Call it in `_get_or_create_session`**

In `_get_or_create_session`, after the session-brief block (after line 570, `except Exception: pass`) and before `SESSIONS[case_id] = sess` (line 571), add:

```python
        _restore_session_state(sess, case_id)
```

(Restore runs AFTER the brief emit — order is fine; the brief reads Amem case memory, independent of RAM.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_session_restore.py -v`
Expected: PASS (3 passed). Also confirm `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import server"` still imports.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/memory/test_session_restore.py
git commit -m "feat(session): restore qa_cache/specialist_kb/input_history on case open"
```

---

## Task 4: Clear-History purges Amem; partial rewind re-snapshots

**Files:**
- Modify: `server.py` (`post_rewind` full branch near line 1102-1107; partial branch after line 1033)
- Test: `tests/memory/test_rewind_memory_semantics.py`

**Interfaces:**
- Consumes: `memory.delete_case_memory` (Task 2); `_NODE_TRACE_STORE.snapshot_session` (existing).
- Produces: full rewind also calls `delete_case_memory(_AMEM, _AMEM_CFG, case_id=case_id)`; partial rewind writes a fresh snapshot of the reduced state.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_rewind_memory_semantics.py
"""Source-level guards: the full-rewind branch purges Amem, the partial branch
re-snapshots. (Full HTTP exercise needs live bootstrap; these assert the wiring
is present so review + the e2e smoke can verify behavior.)"""
import inspect
import server


def test_full_rewind_calls_delete_case_memory():
    src = inspect.getsource(server.post_rewind)
    assert "delete_case_memory(" in src
    # and it's in the full (else) branch, not the partial branch
    assert "delete_case_memory(_AMEM, _AMEM_CFG, case_id=case_id)" in src


def test_partial_rewind_resnapshots():
    src = inspect.getsource(server.post_rewind)
    assert "snapshot_session(" in src   # partial branch re-snapshots reduced state


def test_delete_case_memory_imported():
    from server import delete_case_memory  # re-exported/imported into server
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_rewind_memory_semantics.py -v`
Expected: FAIL — `delete_case_memory(` not in source / import error.

- [ ] **Step 3: Import `delete_case_memory` in server.py**

Extend the existing memory import in `server.py` (currently `from memory import AmemConfig, build_amem_manager, build_session_brief, delete_turns`) to add `delete_case_memory`:

```python
from memory import (AmemConfig, build_amem_manager, build_session_brief,
                    delete_case_memory, delete_turns)
```

- [ ] **Step 4: Purge Amem on full rewind**

In `post_rewind`, at the trace-cleanup block (lines 1101-1107), the full branch already calls `_NODE_TRACE_STORE.delete_case(case_id)`. Immediately after that `else` branch's `delete_case`, add the Amem purge. Change:

```python
    trace_rows_cleared = 0
    if _NODE_TRACE_STORE is not None:
        if is_partial:
            trace_rows_cleared = _NODE_TRACE_STORE.delete_turns(
                remove_turn_ids)
        else:
            trace_rows_cleared = _NODE_TRACE_STORE.delete_case(case_id)
    if not is_partial:
        try:
            delete_case_memory(_AMEM, _AMEM_CFG, case_id=case_id)
        except Exception:
            pass
```

- [ ] **Step 5: Re-snapshot on partial rewind**

In `post_rewind`, in the `is_partial` branch, after the Amem `delete_turns` call (after line 1033), write a fresh snapshot of the reduced RAM so a restart restores the post-rewind state:

```python
        if _NODE_TRACE_STORE is not None:
            try:
                _max_seq = max((e.get("turn_seq", 0) for e in sess.qa_cache.values()
                                if isinstance(e, dict)), default=0)
                _NODE_TRACE_STORE.snapshot_session(
                    chat_id=sess.logger.session_id, case_id=case_id,
                    turn_id=f"rewind-{_max_seq}",
                    qa_cache=sess.qa_cache, specialist_kb=sess.specialist_kb,
                    input_history=sess.input_history)
            except Exception:
                pass
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_rewind_memory_semantics.py -v`
Expected: PASS (3 passed). Confirm `import server` still works.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/memory/test_rewind_memory_semantics.py
git commit -m "feat(session): Clear History purges case Amem; partial rewind re-snapshots"
```

---

## Task 5: `GET /api/cases/<case_id>/history`

**Files:**
- Modify: `server.py` (new route near the other `/api/cases/<case_id>/...` routes)
- Test: `tests/memory/test_history_endpoint.py`

**Interfaces:**
- Produces: `GET /api/cases/<case_id>/history` → `{"messages": [{"id","role","text","turn_id"}, ...]}` reconstructed from the (restored) `sess.qa_cache`, ordered by `turn_seq` ascending, two messages per turn (reviewer question, agent answer). Helper `_history_messages(qa_cache) -> list[dict]` for testability.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_history_endpoint.py
import server


def test_history_messages_reconstructs_ordered_pairs():
    qa = {
        "k2": {"origin_question": "Second?", "answer": "A2",
               "turn_id_origin": "t2", "turn_seq": 2},
        "k1": {"origin_question": "First?", "answer": "A1",
               "turn_id_origin": "t1", "turn_seq": 1},
    }
    msgs = server._history_messages(qa)
    # ordered by turn_seq; reviewer then agent per turn
    assert [m["text"] for m in msgs] == ["First?", "A1", "Second?", "A2"]
    assert msgs[0]["role"] == "reviewer" and msgs[1]["role"] == "agent"
    assert msgs[0]["turn_id"] == "t1" and msgs[1]["turn_id"] == "t1"
    # ids are deterministic and dedup-friendly
    assert msgs[1]["id"] == "hist:t1:agent"


def test_history_messages_empty():
    assert server._history_messages({}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_history_endpoint.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_history_messages'`.

- [ ] **Step 3: Add the helper + route**

In `server.py`, add the helper (module level):

```python
def _history_messages(qa_cache: dict) -> list[dict]:
    """Reconstruct the visible thread from qa_cache, ordered by turn_seq.
    Two messages per turn: the reviewer question then the agent answer."""
    entries = sorted(
        (e for e in (qa_cache or {}).values() if isinstance(e, dict)),
        key=lambda e: e.get("turn_seq", 0))
    out: list[dict] = []
    for e in entries:
        tid = e.get("turn_id_origin") or ""
        q = e.get("origin_question") or ""
        a = e.get("answer") or ""
        if q:
            out.append({"id": f"hist:{tid}:reviewer", "role": "reviewer",
                        "text": q, "turn_id": tid})
        if a:
            out.append({"id": f"hist:{tid}:agent", "role": "agent",
                        "text": a, "turn_id": tid})
    return out
```

And the route (near `post_turn`/`post_rewind`):

```python
@app.get("/api/cases/<case_id>/history")
def get_history(case_id: str):
    try:
        sess = _get_or_create_session(case_id)   # triggers restore
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"messages": _history_messages(sess.qa_cache)})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_history_endpoint.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add server.py tests/memory/test_history_endpoint.py
git commit -m "feat(session): GET /history reconstructs thread from qa_cache"
```

---

## Task 6: Frontend — `fetchHistory`

**Files:**
- Modify: `CaseReviewChat/src/api.ts`; `CaseReviewChat/src/types.ts` (`HistoryResponse`)
- Test: `CaseReviewChat/src/__tests__/api.history.test.ts`

**Interfaces:**
- Produces: `fetchHistory(caseId: string): Promise<Message[]>` — GET `/api/cases/<id>/history`, returns `.messages`. `HistoryResponse = { messages: Message[] }` in `types.ts`.

- [ ] **Step 1: Write the failing test**

```ts
// CaseReviewChat/src/__tests__/api.history.test.ts
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fetchHistory } from '../api'

afterEach(() => vi.restoreAllMocks())

describe('fetchHistory', () => {
  it('GETs /history and returns messages', async () => {
    const messages = [{ id: 'hist:t1:reviewer', role: 'reviewer', text: 'Q', timestamp: 0, turn_id: 't1' }]
    global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ messages }) }) as never
    const out = await fetchHistory('C-1')
    expect(out).toEqual(messages)
    expect(fetch).toHaveBeenCalledWith('/api/cases/C-1/history')
  })

  it('throws on non-ok', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 }) as never
    await expect(fetchHistory('C-1')).rejects.toThrow('fetchHistory failed: 500')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `CaseReviewChat/`): `npx vitest run src/__tests__/api.history.test.ts`
Expected: FAIL — `fetchHistory` is not exported.

- [ ] **Step 3: Add the type + function**

In `src/types.ts` add (near `Message`):

```ts
export type HistoryResponse = { messages: Message[] }
```

In `src/api.ts` add (mirroring `fetchCaseList`):

```ts
export async function fetchHistory(caseId: string): Promise<Message[]> {
  const res = await fetch(`${BASE}/cases/${caseId}/history`)
  if (!res.ok) throw new Error(`fetchHistory failed: ${res.status}`)
  const data = (await res.json()) as HistoryResponse
  return data.messages ?? []
}
```

(Add `Message`/`HistoryResponse` to the existing `import type { ... } from './types'`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/__tests__/api.history.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api.ts src/types.ts src/__tests__/api.history.test.ts
git commit -m "feat(frontend): fetchHistory API"
```

---

## Task 7: Frontend — `clearCaseHistory`, `setCaseHistory`, turn-aware dedup

**Files:**
- Modify: `CaseReviewChat/src/store.ts` (`appendMessage`, new actions); `CaseReviewChat/src/types.ts` (action sigs)
- Test: `CaseReviewChat/src/__tests__/store.session.test.ts`

**Interfaces:**
- Produces:
  - `clearCaseHistory(caseId: string): void` — clears only `threads[caseId]`, `turns[caseId]`, `activeTurnId[caseId]`, removes `caseId` from `unread`.
  - `setCaseHistory(caseId: string, messages: Message[]): void` — replaces `threads[caseId]` with `messages` (server-authoritative).
  - `appendMessage` also dedups by `(turn_id, role)` for agent messages, so SSE replay of a turn already loaded from history doesn't duplicate.

- [ ] **Step 1: Write the failing test**

```ts
// CaseReviewChat/src/__tests__/store.session.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { useStore } from '../store'
import type { Message } from '../types'

const m = (id: string, role: 'agent' | 'reviewer', turn_id?: string): Message =>
  ({ id, role, text: `t-${id}`, timestamp: Date.now(), turn_id })

beforeEach(() => {
  useStore.setState({ threads: {}, turns: {}, activeTurnId: {}, unread: new Set() })
  localStorage.clear()
})

describe('clearCaseHistory', () => {
  it('clears only the target case', () => {
    useStore.setState({ threads: { A: [m('a1', 'agent')], B: [m('b1', 'agent')] } })
    useStore.getState().clearCaseHistory('A')
    expect(useStore.getState().threads.A ?? []).toHaveLength(0)
    expect(useStore.getState().threads.B).toHaveLength(1)   // untouched
  })
})

describe('setCaseHistory + dedup', () => {
  it('replaces the thread and SSE replay does not duplicate by (turn_id, role)', () => {
    const s = useStore.getState()
    s.setCaseHistory('A', [m('hist:t1:agent', 'agent', 't1')])
    // SSE later replays the same turn's agent message with a DIFFERENT id:
    s.appendMessage('A', m('sse-xyz', 'agent', 't1'))
    expect(useStore.getState().threads.A).toHaveLength(1)   // deduped by (turn_id, role)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/__tests__/store.session.test.ts`
Expected: FAIL — `clearCaseHistory`/`setCaseHistory` undefined.

- [ ] **Step 3: Add the actions + dedup**

In `src/store.ts`, add the two actions (near `clearHistory`):

```ts
      clearCaseHistory: (caseId) =>
        set((state) => {
          const threads = { ...state.threads }; delete threads[caseId]
          const turns = { ...state.turns }; delete turns[caseId]
          const activeTurnId = { ...state.activeTurnId }; delete activeTurnId[caseId]
          const unread = new Set(state.unread); unread.delete(caseId)
          return { threads, turns, activeTurnId, unread }
        }),

      setCaseHistory: (caseId, messages) =>
        set((state) => ({ threads: { ...state.threads, [caseId]: messages } })),
```

Update `appendMessage`'s dedup to also skip a `(turn_id, role)` match for agent messages (add after the existing `msg.id` guard):

```ts
          if (msg.id && thread.some((x) => x.id === msg.id)) return {}
          if (msg.turn_id && msg.role === 'agent' &&
              thread.some((x) => x.turn_id === msg.turn_id && x.role === 'agent')) return {}
```

In `src/types.ts`, add to `StoreState`:

```ts
  clearCaseHistory: (caseId: string) => void
  setCaseHistory: (caseId: string, messages: Message[]) => void
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/__tests__/store.session.test.ts`
Expected: PASS.

- [ ] **Step 5: Regression**

Run: `npx vitest run src/__tests__/store.test.ts`
Expected: PASS (existing store tests unaffected — `appendMessage` id-dedup still holds).

- [ ] **Step 6: Commit**

```bash
git add src/store.ts src/types.ts src/__tests__/store.session.test.ts
git commit -m "feat(frontend): per-case clearCaseHistory + setCaseHistory + turn-aware dedup"
```

---

## Task 8: Frontend — per-case Clear History button

**Files:**
- Modify: `CaseReviewChat/src/components/Sidebar/Sidebar.tsx`
- Test: `CaseReviewChat/src/__tests__/sidebar.clear.test.tsx` (or extend store tests if the component is hard to mount)

**Interfaces:**
- Consumes: `clearCaseHistory` (Task 7), `postRewind` (existing), `activeCase`.

- [ ] **Step 1: Write the failing test**

```tsx
// CaseReviewChat/src/__tests__/sidebar.clear.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useStore } from '../store'

vi.mock('../api', () => ({ postRewind: vi.fn().mockResolvedValue(undefined) }))
import { postRewind } from '../api'
import { handleClearHistoryForActive } from '../components/Sidebar/Sidebar'

beforeEach(() => {
  useStore.setState({ activeCase: 'A', threads: { A: [], B: [] }, turns: {}, activeTurnId: {}, unread: new Set() })
  vi.clearAllMocks()
})

it('rewinds only the active case and clears only its thread', async () => {
  await handleClearHistoryForActive()
  expect(postRewind).toHaveBeenCalledTimes(1)
  expect(postRewind).toHaveBeenCalledWith('A', '')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/__tests__/sidebar.clear.test.tsx`
Expected: FAIL — `handleClearHistoryForActive` not exported.

- [ ] **Step 3: Rewrite the handler (per-case) and export it**

In `src/components/Sidebar/Sidebar.tsx`, replace the global `handleClearHistory` (lines 24-32) with a per-case version that reads `activeCase` + `clearCaseHistory` from the store, and export a testable standalone:

```tsx
export async function handleClearHistoryForActive() {
  const { activeCase, clearCaseHistory } = useStore.getState()
  if (!activeCase) return
  await postRewind(activeCase, '').catch((err) =>
    console.error(`Failed to clear server cache for case ${activeCase}`, err))
  clearCaseHistory(activeCase)
}
```

Wire the button's `onClick` to `handleClearHistoryForActive` and update its label to "Clear this case" (find the button that currently calls `handleClearHistory`). Remove the old global `handleClearHistory`. If `clearHistory` (global store action) is now unused (grep confirms only Sidebar used it), it can stay for compatibility or be removed — leave it to avoid a wider change unless the reviewer prefers removal.

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/__tests__/sidebar.clear.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/components/Sidebar/Sidebar.tsx src/__tests__/sidebar.clear.test.tsx
git commit -m "feat(frontend): per-case Clear History"
```

---

## Task 9: Frontend — load history on case open

**Files:**
- Create: `CaseReviewChat/src/hooks/useCaseHistory.ts`
- Modify: `CaseReviewChat/src/components/ChatPanel/ChatPanel.tsx` (call the hook alongside `useSSE`)
- Test: `CaseReviewChat/src/__tests__/useCaseHistory.test.ts`

**Interfaces:**
- Consumes: `fetchHistory` (Task 6), `setCaseHistory` (Task 7).
- Produces: `useCaseHistory(caseId: string | null)` — on `caseId` change, fetch history and `setCaseHistory(caseId, messages)`; ignores errors (leaves the persisted thread as fallback).

- [ ] **Step 1: Write the failing test**

```ts
// CaseReviewChat/src/__tests__/useCaseHistory.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useStore } from '../store'

vi.mock('../api', () => ({
  fetchHistory: vi.fn().mockResolvedValue([
    { id: 'hist:t1:agent', role: 'agent', text: 'A1', timestamp: 0, turn_id: 't1' },
  ]),
}))
import { fetchHistory } from '../api'
import { useCaseHistory } from '../hooks/useCaseHistory'

beforeEach(() => {
  useStore.setState({ threads: {}, turns: {}, activeTurnId: {}, unread: new Set() })
  vi.clearAllMocks()
})

it('loads history into the thread on case open', async () => {
  renderHook(() => useCaseHistory('A'))
  await waitFor(() => expect(fetchHistory).toHaveBeenCalledWith('A'))
  await waitFor(() => expect(useStore.getState().threads.A).toHaveLength(1))
})

it('is a no-op when caseId is null', () => {
  renderHook(() => useCaseHistory(null))
  expect(fetchHistory).not.toHaveBeenCalled()
})
```

(If `@testing-library/react` isn't installed, check `package.json`; the repo's `ChatPanel.test.tsx` implies a React test setup exists — mirror its render approach. If hooks can't be rendered, extract the effect body into a plain async `loadCaseHistory(caseId)` function and test that directly instead.)

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/__tests__/useCaseHistory.test.ts`
Expected: FAIL — `useCaseHistory` missing.

- [ ] **Step 3: Implement the hook**

```ts
// CaseReviewChat/src/hooks/useCaseHistory.ts
import { useEffect } from 'react'
import { fetchHistory } from '../api'
import { useStore } from '../store'

/** On case open, load the server-authoritative thread. The persisted store
 *  (localStorage) provides an instant first paint; this replaces it with the
 *  server's truth so restarts / other devices show the real conversation.
 *  Errors are ignored — the persisted thread remains as fallback. */
export function useCaseHistory(caseId: string | null): void {
  const setCaseHistory = useStore((s) => s.setCaseHistory)
  useEffect(() => {
    if (!caseId) return
    let cancelled = false
    fetchHistory(caseId)
      .then((messages) => { if (!cancelled) setCaseHistory(caseId, messages) })
      .catch(() => { /* keep persisted thread */ })
    return () => { cancelled = true }
  }, [caseId, setCaseHistory])
}
```

- [ ] **Step 4: Call it in ChatPanel**

In `src/components/ChatPanel/ChatPanel.tsx`, next to `useSSE(activeCase)` (line 40), add:

```tsx
  useCaseHistory(activeCase)
```

and import it: `import { useCaseHistory } from '../../hooks/useCaseHistory'`.

- [ ] **Step 5: Run test to verify it passes**

Run: `npx vitest run src/__tests__/useCaseHistory.test.ts`
Expected: PASS.

- [ ] **Step 6: Full frontend regression**

Run: `npx vitest run`
Expected: PASS (all frontend specs green — history load + SSE replay dedup by `(turn_id, role)` prevents duplicates).

- [ ] **Step 7: Commit**

```bash
git add src/hooks/useCaseHistory.ts src/components/ChatPanel/ChatPanel.tsx src/__tests__/useCaseHistory.test.ts
git commit -m "feat(frontend): load server-authoritative history on case open"
```

---

## Task 10: End-to-end live verification (manual; Qdrant + both servers)

**Files:** none (operational).

- [ ] **Step 1: Start Qdrant + backend on 49002**

```bash
docker start amem-qdrant 2>/dev/null || docker run -d --name amem-qdrant -p 6333:6333 -p 6334:6334 \
  -v "$PWD/.runtime/qdrant/storage:/qdrant/storage" qdrant/qdrant:v1.18.3
AMEM_STORE_URL=http://127.0.0.1:6333 PORT=49002 ~/.pyenv/versions/3.11.13/envs/autoAI/bin/python server.py
```

- [ ] **Step 2: Drive a turn, then restart, then verify continuity**

1. Ask a question on a case in the UI (or `curl -X POST .../api/cases/<id>/message -d '{"text":"..."}'`); wait for the answer.
2. `curl http://localhost:49002/api/cases/<id>/history` → confirm the reviewer+agent messages are returned.
3. **Restart** the backend (Ctrl-C, rerun the Step-1 command).
4. `curl http://localhost:49002/api/cases/<id>/history` again → the conversation is still there (restored from snapshot).
5. Ask a follow-up that references the earlier turn ("what did you just say about X?") → the agent has context (episodic + Amem restored).

- [ ] **Step 3: Verify Clear History purges; Rewind removes a turn**

1. `curl -X POST .../api/cases/<id>/rewind -d '{}'` (Clear History) → `curl .../history` returns empty; restart → still empty; a re-asked question does NOT replay and the case-summary brief reflects no prior learnings (Amem purged).
2. Ask two turns, then `curl -X POST .../api/cases/<id>/rewind -d '{"removeTurnIds":["<turn2_id>"]}'` → history shows only turn 1; restart → still only turn 1.

- [ ] **Step 4: Record the result** in the PR/description (or note Docker-unavailable + skipped). No commit.

---

## Self-Review

**Spec coverage** (spec § → task):
- §4 restore + raw-qa_cache snapshot → Tasks 1, 3.
- §5 snapshot consistency (full delete_case already wired; add Amem purge; partial re-snapshot) → Tasks 1, 4.
- §6 history endpoint → Task 5.
- §7 Clear History purges Amem; Rewind purges undone turns (Phase-1 `delete_turns` retained) → Tasks 2, 4.
- §8 per-case Clear History (frontend) → Tasks 7, 8.
- §9 load thread on case open → Tasks 6, 7, 9.
- §10 Amem interaction unchanged; episodic restored via qa_cache → Tasks 1, 3.
- §11 edge cases (defensive decode, no-store, server-authoritative merge) → Tasks 1, 3, 7, 9.
- §12 testing → every task; §10 live e2e.

**Placeholder scan:** none. The two "if X isn't installed / hard to mount" notes (Tasks 9 test tooling) carry a concrete fallback, not deferred work.

**Type/name consistency:** `load_latest_snapshot` return keys (`qa_cache`/`specialist_kb`/`input_history`/`chat_id`) match `_restore_session_state`'s reads (Task 3) and `snapshot_session`'s raw column (Task 1). `delete_case_memory(amem, cfg, *, case_id)` signature identical across Tasks 2, 4. Frontend `setCaseHistory`/`clearCaseHistory`/`fetchHistory`/`useCaseHistory` names consistent across Tasks 6–9. History message id scheme `hist:<turn_id>:<role>` (Task 5) matches the frontend dedup-by-`(turn_id, role)` (Task 7).

**Known follow-ups (out of scope):** discrete/closable sessions + session-scoped Amem (spec §15); a possible `case_id` index on `session_snapshot` if the case grows very large (currently the index is `(chat_id, turn_id, taken_at)` — the `WHERE case_id` scan is fine at current scale, noted in spec §11).
