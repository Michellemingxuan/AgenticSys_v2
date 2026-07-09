# Turn Runner Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the turn-runner refactor — make `runner/` stop importing `server` (I1), extract the per-item SSE mapping (M4), and add a direct `TurnRunner` SSE test (M5).

**Architecture:** Relocate the server-defined turn-leaf helpers into `runner/turn/{cache,finalize}.py` and the watchdog into `conductor.py`; move the shared constants + `_NODE_TRACE_STORE` singleton into a neutral `runner/config.py` that both `server.py` and `conductor.py` import. Then extract `_run_orchestrator`'s RunItem→SSE mapping into `runner/turn/sse.py`, and lock the cache-replay SSE set with a direct test.

**Tech Stack:** Python 3.11 (pyenv virtualenv `autoAI`), openai-agents 0.3.3, pytest 8.4.2.

## Global Constraints

- **Behavior-preserving.** No observable behavior/signature changes. Moves are verbatim; references change module, not logic.
- **Interpreter:** run pytest with `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest`. Bare python lacks matplotlib → collection errors.
- **Baseline:** `pytest tests/ -q` is **529 passed** today. Each task must leave it at 529 (+ any new tests), zero collection errors, pristine output.
- **One-way dependency:** after Task 4, `grep -rn "from server import\|import server\b" runner/ --include=*.py` → none. `runner/` never imports `server`.
- **Do NOT touch** the SDK loop (`agents/`) or the `llm/` firewall.
- **Do NOT commit `brainstorm/*` or `.superpowers/*`.** Stage each task's files explicitly.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Stage 1 — I1: make `runner/` stop importing `server`

### Task 1: `runner/config.py` — shared constants + node-trace singleton

**Files:**
- Create: `runner/config.py`
- Modify: `server.py` (replace the constant/singleton *definitions* with imports from `runner/config.py`; keep the startup `print`), `runner/turn/conductor.py` (import these from `runner/config.py` instead of `server`)

**Interfaces:**
- Produces (module-level values, verbatim same names/types/defaults): `PILLAR`, `_ORCH_PLAN_TIMEOUT_S`, `_SCREEN_TIMEOUT_S`, `_PRIOR_QUESTIONS_FOR_SCREEN`, `_REPORTS_DIR`, `_NODE_TRACE_DB_PATH`, `_NODE_TRACE_STORE`.

- [ ] **Step 1: Baseline**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `529 passed`.

- [ ] **Step 2: Create `runner/config.py`**

Move these definitions **verbatim** from `server.py` (current lines: `PILLAR`:67, `_ORCH_PLAN_TIMEOUT_S`:113, `_SCREEN_TIMEOUT_S`:126, `_PRIOR_QUESTIONS_FOR_SCREEN`:136, `_REPORTS_DIR` (grep it: `grep -n "_REPORTS_DIR *=" server.py`), `_NODE_TRACE_DB_PATH`:147, `_NODE_TRACE_STORE`:150). Header:

```python
"""Shared runtime config for the server + turn runner — env-derived constants
and the node-trace singleton. A neutral module both `server.py` and
`runner/turn/*` import, so `runner/` never imports `server`."""
from __future__ import annotations

import os
from pathlib import Path

from tools.node_trace import NodeTraceStore  # adjust to the actual import path used in server.py

PILLAR = os.environ.get("PILLAR", "credit_risk")
_ORCH_PLAN_TIMEOUT_S = float(os.environ.get("ORCH_PLAN_TIMEOUT_S", "25"))
_SCREEN_TIMEOUT_S = float(os.environ.get("SCREEN_TIMEOUT_S", "30"))
# _PRIOR_QUESTIONS_FOR_SCREEN, _REPORTS_DIR — paste the exact multi-line defs from server.py
_NODE_TRACE_DB_PATH = os.path.expanduser(os.path.expandvars(
    os.environ.get("NODE_TRACE_DB", "logs/node_traces.db")
))
_NODE_TRACE_STORE: NodeTraceStore | None = (
    NodeTraceStore(db_path=_NODE_TRACE_DB_PATH)
    if os.environ.get("NODE_TRACE_DISABLE") != "1"
    else None
)
```
Verify the `NodeTraceStore` import path matches how `server.py` imports it (`grep -n "NodeTraceStore" server.py`). Copy `_PRIOR_QUESTIONS_FOR_SCREEN` and `_REPORTS_DIR` bodies verbatim (they are multi-line).

- [ ] **Step 3: Rewire `server.py`**

Delete those 7 definitions from `server.py`; add `from runner.config import (PILLAR, _NODE_TRACE_DB_PATH, _NODE_TRACE_STORE, _ORCH_PLAN_TIMEOUT_S, _PRIOR_QUESTIONS_FOR_SCREEN, _REPORTS_DIR, _SCREEN_TIMEOUT_S)`. **Keep** the import-time startup `print(...)` block (server.py:155–164) exactly where it is — it references the now-imported `_NODE_TRACE_STORE`/`_NODE_TRACE_DB_PATH`, which resolve fine. Remove the now-unused `NodeTraceStore` import from server.py only if nothing else there uses it (`grep -n "NodeTraceStore" server.py`).

- [ ] **Step 4: Point conductor's constant imports at config**

In `runner/turn/conductor.py`, the `from server import (…)` block currently pulls `PILLAR`, `_NODE_TRACE_STORE`, `_ORCH_PLAN_TIMEOUT_S`, `_PRIOR_QUESTIONS_FOR_SCREEN`, `_REPORTS_DIR`, `_SCREEN_TIMEOUT_S`. Remove those six from the `from server import` list and add `from runner.config import (PILLAR, _NODE_TRACE_STORE, _ORCH_PLAN_TIMEOUT_S, _PRIOR_QUESTIONS_FOR_SCREEN, _REPORTS_DIR, _SCREEN_TIMEOUT_S)`. Leave the remaining `from server import` names (the helpers + watchdog) for Tasks 2–4.

- [ ] **Step 5: Import + full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import runner.config, server, runner.turn.conductor; print('OK')"`
Expected: `OK` (+ the two `[server] node_trace …` prints).
Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `529 passed`.

- [ ] **Step 6: Commit**

```bash
git add runner/config.py server.py runner/turn/conductor.py
git commit -m "refactor(runner): move shared constants + node-trace singleton to runner/config.py

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `runner/turn/cache.py` — Q→A cache + KP lookup helpers

**Files:**
- Create: `runner/turn/cache.py`
- Modify: `server.py` (remove the 4 helper defs), `runner/turn/conductor.py` (import from cache), tests that use `server._normalize_q/_get_cached_qa/_store_cached_qa/_find_kp`

**Interfaces:**
- Produces (verbatim signatures): `_normalize_q`, `_get_cached_qa`, `_store_cached_qa`, `_find_kp`.

- [ ] **Step 1: Move the four functions verbatim**

Move `_normalize_q` (server.py:496), `_get_cached_qa` (507), `_store_cached_qa` (526), `_find_kp` (716) verbatim into `runner/turn/cache.py`. Carry their imports (check each function body for `json`, `time`, typing, etc.; `grep`-confirm no reference to a `server`-only global — if one exists, it must come from `runner/config.py` or be passed in). Module docstring: `"""Layer-1 prior-turn recall: Q→A exact-match cache + KnowledgePoint lookup."""`

- [ ] **Step 2: Rewire conductor + server**

In `conductor.py`, drop `_normalize_q/_get_cached_qa/_store_cached_qa/_find_kp` from the `from server import` list; add `from runner.turn.cache import _find_kp, _get_cached_qa, _normalize_q, _store_cached_qa`. Delete the four defs from `server.py` (they have 0 non-def uses there).

- [ ] **Step 3: Repoint tests**

Find every test reference: `grep -rn "server\._normalize_q\|server\._get_cached_qa\|server\._store_cached_qa\|server\._find_kp\|from server import.*\(_normalize_q\|_get_cached_qa\|_store_cached_qa\|_find_kp\)" tests/`. In each (notably `tests/test_server.py`, `tests/test_tools/test_episodic_turn_seq.py`), change the reference to import from `runner.turn.cache` (e.g. `from runner.turn.cache import _get_cached_qa` and call `_get_cached_qa(...)`, or `import runner.turn.cache as cache` + `cache._get_cached_qa`). Assertions unchanged.

- [ ] **Step 4: Grep gate + suite**

Run: `grep -rn "server\._normalize_q\|server\._get_cached_qa\|server\._store_cached_qa\|server\._find_kp" tests/`
Expected: no output.
Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `529 passed`.

- [ ] **Step 5: Commit**

```bash
git add runner/turn/cache.py server.py runner/turn/conductor.py tests/
git commit -m "refactor(runner): relocate Q->A cache + KP helpers to runner/turn/cache.py

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `runner/turn/finalize.py` — chart collection + fallback/salvage

**Files:**
- Create: `runner/turn/finalize.py`
- Modify: `server.py` (remove 3 defs), `runner/turn/conductor.py` (import from finalize), tests using `server._collect_turn_charts/_synthesize_fallback_answer/_replay_completed_specialists`

**Interfaces:**
- Produces (verbatim signatures): `_collect_turn_charts`, `_synthesize_fallback_answer`, `_replay_completed_specialists`.

- [ ] **Step 1: Move the three functions verbatim**

Move `_synthesize_fallback_answer` (server.py:386), `_replay_completed_specialists` (467), `_collect_turn_charts` (601) verbatim into `runner/turn/finalize.py`. Carry their imports (check bodies; e.g. `_collect_turn_charts` likely touches viz/KP structures — grep-confirm no `server`-only global). Docstring: `"""Turn finalization: chart collection + fallback/salvage answer synthesis."""`

- [ ] **Step 2: Rewire conductor + server**

In `conductor.py`, drop those three from `from server import`; add `from runner.turn.finalize import _collect_turn_charts, _replay_completed_specialists, _synthesize_fallback_answer`. Delete the three defs from `server.py`.

- [ ] **Step 3: Repoint tests**

`grep -rn "server\._collect_turn_charts\|server\._synthesize_fallback_answer\|server\._replay_completed_specialists" tests/` (mostly `tests/test_server.py`, ~13 `_collect_turn_charts` sites). Repoint to `runner.turn.finalize`. Assertions unchanged.

- [ ] **Step 4: Grep gate + suite**

Run: `grep -rn "server\._collect_turn_charts\|server\._synthesize_fallback_answer\|server\._replay_completed_specialists" tests/`
Expected: no output.
Run: full suite → `529 passed`.

- [ ] **Step 5: Commit**

```bash
git add runner/turn/finalize.py server.py runner/turn/conductor.py tests/
git commit -m "refactor(runner): relocate chart-collection + fallback helpers to runner/turn/finalize.py

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: watchdog into `conductor.py`; delete `from server import` (completes I1)

**Files:**
- Modify: `runner/turn/conductor.py` (move watchdog in, remove the server import), `server.py` (remove watchdog defs), tests using `server._next_planning_event/_PlanningTimeout`

**Interfaces:**
- Produces: `_PlanningTimeout`, `_TurnAborted` (exceptions), `_next_planning_event` now defined at module level in `conductor.py`.

- [ ] **Step 1: Move the watchdog verbatim into conductor.py**

Move `_PlanningTimeout` (class), `_TurnAborted` (class), and `_next_planning_event` (async def) verbatim from `server.py` (grep: `grep -n "class _PlanningTimeout\|class _TurnAborted\|async def _next_planning_event" server.py`) to module level in `runner/turn/conductor.py` (above the `TurnRunner` class). Remove them from the `from server import` list. After this, the `from server import (…)` block should be **empty** — delete the whole statement. Delete the three defs from `server.py`.

- [ ] **Step 2: I1 grep gate**

Run: `grep -rn "from server import\|import server\b" runner/ --include="*.py"`
Expected: **no output** (runner/ no longer imports server).
Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import runner.turn.conductor; print('no cycle, OK')"`
Expected: `no cycle, OK`.

- [ ] **Step 3: Repoint tests**

`grep -rn "server\._next_planning_event\|server\._PlanningTimeout\|server\._TurnAborted" tests/` → repoint to `runner.turn.conductor`. Assertions unchanged.

- [ ] **Step 4: Full suite**

Run: full suite → `529 passed`, zero collection errors.

- [ ] **Step 5: Commit**

```bash
git add runner/turn/conductor.py server.py tests/
git commit -m "refactor(runner): move planning watchdog into conductor; drop from-server import (one-way dep)

conductor.py no longer imports server. Dependency now points one way:
server -> runner -> config/leaves.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Stage 2 — M4: extract the per-item SSE mapping

### Task 5: `runner/turn/sse.py` — RunItem → typed SSE events

**Files:**
- Create: `runner/turn/sse.py`
- Modify: `runner/turn/conductor.py` (`_run_orchestrator` calls the extracted mapper)

**Interfaces:**
- Produces: `map_run_item(item, *, sess, turn_id, tool_calls, call_index_by_id, started_at_by_call, team_plan_emitted, emit) -> bool` — translates one streamed `RunItem` into the typed SSE events (`team_plan`/`agent_started`/`agent_completed`) via the passed `emit` callable, mutating the passed emit-state dicts, and returns the (possibly updated) `team_plan_emitted` flag. Exact parameter set: mirror precisely the locals the current mapping block reads/writes — enumerate them from the code before writing the signature.

- [ ] **Step 1: Locate the mapping block**

In `conductor.py::_run_orchestrator`, find the per-item translation inside the stream loop — the `if isinstance(it, ToolCallItem): … elif isinstance(it, ToolCallOutputItem): … elif isinstance(it, MessageOutputItem): …` block that emits `team_plan`/`agent_started`/`agent_completed` and mutates `self.tool_calls`, `self.call_index_by_id`, `self.started_at_by_call`, `self.team_plan_emitted`. Read it fully; list every `self.` field and every `sess.emit(...)` it touches.

- [ ] **Step 2: Extract verbatim into `runner/turn/sse.py`**

Create `runner/turn/sse.py` with `map_run_item(...)` whose body is the mapping block **verbatim**, with `self.X` rewritten to the passed parameters (dicts are mutated in place; the `team_plan_emitted` bool is returned since bools don't mutate). Module docstring: `"""RunItem → typed SSE event mapping for the orchestrator stream drive."""` Import the SDK item types (`from agents.items import ToolCallItem, ToolCallOutputItem, MessageOutputItem` — match conductor's existing import).

- [ ] **Step 3: Call it from `_run_orchestrator`**

Replace the inline block with:
```python
self.team_plan_emitted = map_run_item(
    it, sess=self.sess, turn_id=self.turn_id,
    tool_calls=self.tool_calls, call_index_by_id=self.call_index_by_id,
    started_at_by_call=self.started_at_by_call,
    team_plan_emitted=self.team_plan_emitted, emit=self.sess.emit)
```
(Adjust the arg list to exactly the fields Step 1 enumerated.) Add `from runner.turn.sse import map_run_item`.

- [ ] **Step 4: Full suite + SSE spot-check**

Run: full suite → `529 passed`. The existing SSE/dispatch tests (`tests/test_server_dispatch/`, `tests/test_server.py`) guard the emitted events — they must stay green. If the extraction's parameter threading gets awkward (emit-state mutation unclear), STOP: report DONE_WITH_CONCERNS and leave the mapping inline (spec §5 permits M4 = `_finalize`-only).

- [ ] **Step 5: Commit**

```bash
git add runner/turn/sse.py runner/turn/conductor.py
git commit -m "refactor(runner): extract RunItem->SSE mapping to runner/turn/sse.py

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Stage 3 — M5: direct TurnRunner cache-replay SSE test

### Task 6: `tests/test_runner/test_conductor_sse.py`

**Files:**
- Create: `tests/test_runner/test_conductor_sse.py`

**Interfaces:**
- Consumes: `runner.turn.conductor.TurnRunner`.

- [ ] **Step 1: Write the test**

Create `tests/test_runner/test_conductor_sse.py`. Build a fake `sess` recording `emit` calls and a `qa_cache` with one prior turn (tool_calls + a chart), then drive `_replay_from_cache`:

```python
import asyncio
import types
import pytest
from runner.turn.conductor import TurnRunner


class _FakeSess:
    def __init__(self, qa_cache):
        self.events = []
        self.qa_cache = qa_cache
        self.specialist_kb = {}
        self.logger = types.SimpleNamespace(log=lambda *a, **k: None)
        # add any other attributes TurnRunner.__init__/_replay_from_cache reads
    def emit(self, event, payload):
        self.events.append(event)


def _seeded_cache():
    # shape must match what _store_cached_qa writes / _get_cached_qa reads;
    # inspect runner/turn/cache.py to mirror the real keys (redacted_question,
    # answer, tool_calls, charts, turn_seq, …).
    ...  # build one entry keyed by _normalize_q(question)


@pytest.mark.asyncio
async def test_cache_replay_emits_full_sse_set():
    sess = _FakeSess(_seeded_cache())
    runner = TurnRunner(sess, turn_id="t1", question="the cached question")
    # set runner.verdict so cache lookup key matches (mirror _screen's output)
    replayed = await runner._replay_from_cache()
    assert replayed is True
    assert sess.events == [
        "team_plan", "agent_started", "agent_completed", "chart", "final",
    ]


@pytest.mark.asyncio
async def test_cache_miss_emits_nothing_and_returns_false():
    sess = _FakeSess({})
    runner = TurnRunner(sess, turn_id="t2", question="unseen question")
    assert await runner._replay_from_cache() is False
    assert sess.events == []
```

**Before finalizing:** read `runner/turn/conductor.py::_replay_from_cache` and `runner/turn/cache.py` to mirror the exact `sess`/`verdict` attributes and `qa_cache` entry shape the method reads (this is the only non-obvious part — the fakes must match reality or the test is vacuous). Fill the `...` placeholders with the real structure; do not leave them.

- [ ] **Step 2: Run**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_conductor_sse.py -v`
Expected: both PASS. If `_replay_from_cache` needs more `sess`/`ctx` wiring than the fake provides, extend the fake minimally until the real replay path runs (do NOT stub out the method under test).

- [ ] **Step 3: Full suite + commit**

Run: full suite → `529 + 2 passed`.
```bash
git add tests/test_runner/test_conductor_sse.py
git commit -m "test(runner): assert TurnRunner cache-replay emits the full SSE set

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Verification summary

- Full suite green (**529** + new tests) under the autoAI interpreter at each task.
- I1 gate (after Task 4): `grep -rn "from server import\|import server\b" runner/ --include=*.py` → none; `import runner.turn.conductor` succeeds.
- Constant single-source: env-read constants + `_NODE_TRACE_STORE` defined once in `runner/config.py`; server imports them.
- Do NOT commit `brainstorm/*` or `.superpowers/*`.
