# Turn Runner Follow-ups — Design Spec

Date: 2026-07-09
Status: Draft (for review)
Owner: runner / server
Prior: `docs/superpowers/specs/2026-07-09-turn-runner-pipeline-design.md` (the refactor these follow-ups finish)

## 1. Problem

The turn-runner refactor (merged `e513d8d`) delivered a readable `TurnRunner`
spine but left three items flagged by the final whole-branch review:

- **I1 — dependency direction not one-way.** `runner/turn/conductor.py` imports
  16 names *from* `server.py`. Safe today (no cycle — `server` imports
  `conductor` lazily), but the design intended `runner/` never to import
  `server`. The tangle was relocated, not dissolved.
- **M4 — phase bodies still large.** `_run_orchestrator` (~400 lines) and
  `_finalize` (~250) are not yet focused units.
- **M5 — no direct `TurnRunner` SSE test.** The cache-replay / re-dispatch SSE
  re-emit paths — the SSE invariant's highest-risk surface — are only exercised
  indirectly.

Findings live in the (gitignored) SDD ledger and
`.claude/memory/project_turn_runner_followups.md`.

## 2. Goals / Non-goals

**Goals**
- I1: eliminate `conductor.py`'s `from server import (…)` so the dependency
  points one way (`server → runner → config`).
- M4: extract the genuinely pure sub-logic of the two heaviest methods; shrink
  them without dissolving the state-coupled loops.
- M5: a direct `TurnRunner` test asserting the cache-replay full-SSE-replay set.

**Non-goals**
- No behavior/signature changes to observable behavior (I1 + M4 are pure
  structural moves; `nonlocal`/module-global references become module-function
  or `self`/`ctx` reads).
- Do NOT touch the SDK loop or the `llm/` firewall.
- Do NOT force-dissolve `_run_orchestrator`'s retry loop / SSE emit-state — it
  stays a method (see §5).

## 3. Key finding that shapes the design

After the turn body moved to `conductor.py`, **all 9 imported helper functions
are dead in `server.py`'s own code** (0 non-def references; the few
`_PlanningTimeout`/`_TurnAborted` hits are internal cross-refs among the
watchdog helpers). Only `conductor` and tests (via `server._X`) use them. The
imported **constants**, by contrast, are still used by `server.py`'s Flask
routes/bootstrap (`_NODE_TRACE_STORE` at server.py:155/832/1318/1424, plus
`PILLAR`, `_REPORTS_DIR`). So: helpers fully relocate; constants need a neutral
shared home. The `_collect_turn_charts` mentions in `tools/` and
`agent_factories/` are comments only — no lower layer imports these, so
relocation inverts no layering.

## 4. I1 — target structure

**Helper functions → themed `runner/turn/` leaf modules** (removed from
`server.py`; tests repoint from `server._X` to the new module):

| Helpers | New home | Rationale |
|---|---|---|
| `_normalize_q`, `_get_cached_qa`, `_store_cached_qa`, `_find_kp` | `runner/turn/cache.py` | prior-turn Q→A recall + KP lookup |
| `_collect_turn_charts`, `_synthesize_fallback_answer`, `_replay_completed_specialists` | `runner/turn/finalize.py` | finalize/salvage logic — dovetails with M4's `finalize.py` |
| `_next_planning_event`, `_PlanningTimeout`, `_TurnAborted` | `runner/turn/conductor.py` | intrinsic to the stream drive; no consumer but tests |

**Constants → `runner/config.py`** (new): defines `_SCREEN_TIMEOUT_S`,
`_ORCH_PLAN_TIMEOUT_S`, `_PRIOR_QUESTIONS_FOR_SCREEN`, `_REPORTS_DIR`, `PILLAR`
by reading the same env vars with the same defaults currently in `server.py`.
Both `server.py` and `conductor.py` import from it (`server → config`,
`runner → config` — both correct direction). `server.py`'s own definitions are
replaced by imports from `runner/config.py` so there is a single source (no
duplication/drift).

**`_NODE_TRACE_STORE`** (runtime singleton) → also moves into
`runner/config.py`, together with `_NODE_TRACE_DB_PATH`. Its initialization is
self-contained — `NodeTraceStore(db_path=_NODE_TRACE_DB_PATH) if
os.environ.get("NODE_TRACE_DISABLE") != "1" else None` (server.py:150–154) — so
`config.py` can own it as the single source; both `server.py` and `conductor.py`
import it. (An earlier idea to read it from `ctx`/`sess` fails: `conductor`
reads `_NODE_TRACE_STORE` in the screen and cache-replay paths, which run
*before* the `AppContext` is built, so no `ctx` exists yet.) The import-time
startup `print(...)` (server.py:155–164) stays in `server.py`, guarded on the
imported value.

**Result:** `conductor.py` has no `from server import`. Dependency graph:
`server → {runner.turn.conductor, runner.turn.cache, runner.turn.finalize,
runner.config}`; `runner.turn.* → runner.config` and leaves (`tools.*`); never
`runner → server`.

**Test repointing:** `tests/test_server.py` and any other test calling
`server._normalize_q/_get_cached_qa/_store_cached_qa/_find_kp/_collect_turn_charts/
_synthesize_fallback_answer/_replay_completed_specialists/_next_planning_event/
_PlanningTimeout` repoint to the new module (`from runner.turn.cache import …`,
`from runner.turn.finalize import …`, `from runner.turn.conductor import …`).
Verify each with grep before and after.

## 5. M4 — decompose the big methods (conservative)

`_run_orchestrator` is state-coupled (retry loop + SSE emit-state) and cannot
fully dissolve. Extract the one clean chunk:

- **`runner/turn/sse.py`** (new): the per-`RunItem` → typed-SSE-event mapping
  (the `if isinstance(item, ToolCallItem)/ToolCallOutputItem/MessageOutputItem`
  translation block). Signature takes the item plus the emit-state it needs and
  emits/returns the typed events. `_run_orchestrator` calls it per streamed
  item. The retry loop, watchdog, and `_orch_attempt` accounting stay in the
  method.

`_finalize` shrinks because §4 already moves `_collect_turn_charts`,
`_synthesize_fallback_answer`, `_replay_completed_specialists` into
`finalize.py`; what remains (distiller drain + final-payload shaping) stays a
method calling those helpers.

**Explicitly:** `_run_orchestrator` remains the largest method after M4, by
necessity. The win is that its pure event-mapping is now a named, testable unit
and `_finalize` is a thin coordinator. No further forcing.

## 6. M5 — direct TurnRunner SSE test

`tests/test_runner/test_conductor_sse.py` (new): build a `TurnRunner` with a
fake `sess` that records `emit(event, payload)` calls and a seeded `qa_cache`
(one prior turn with tool_calls + a chart), call `await _replay_from_cache()`,
and assert the recorded event sequence is exactly
`team_plan → agent_started → agent_completed → chart → final` (the full replay
set — see `.claude/memory/feedback_alternate_paths_must_replay_full_sse.md`).
Assert the method returned `True` (replay short-circuit). A second case may
cover the empty-cache miss (`False`, no emits). This locks the highest-risk SSE
surface directly rather than through the full turn.

## 7. Staging

Each stage behavior-preserving; run the full suite (autoAI interpreter) — must
stay green (currently **529 passed**) plus any new tests; commit per stage.

- **Stage 1 (I1):** create `runner/config.py` (constants) + repoint server.py's
  own constant references to it; create `runner/turn/cache.py`; move
  chart/fallback helpers into `runner/turn/finalize.py`; move watchdog into
  `conductor.py`; switch `_NODE_TRACE_STORE` to `ctx`/`sess`; delete
  `conductor.py`'s `from server import`; repoint the `server._X` tests. Grep
  gate: `grep -rn "from server import" runner/ --include=*.py` → none.
- **Stage 2 (M4):** create `runner/turn/sse.py`; `_run_orchestrator` calls it
  per item; confirm `_finalize` is a thin coordinator over the §4 helpers.
- **Stage 3 (M5):** add the conductor SSE test.

Stopping after Stage 1 is a valid partial win (it fully resolves the flagged
architectural finding).

## 8. Verification

- `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q` →
  **529 passed** (+ new tests), zero collection errors, pristine output.
  (autoAI interpreter — bare python lacks matplotlib;
  `.claude/memory/dev_env_autoai_interpreter.md`.)
- I1 gate: `grep -rn "from server import\|import server\b" runner/ --include=*.py`
  → none. `python -c "import runner.turn.conductor"` succeeds (no cycle).
- Constant single-source: the env-read constants are defined once in
  `runner/config.py`; `grep -n "_SCREEN_TIMEOUT_S\s*=\|_REPORTS_DIR\s*=\|PILLAR\s*="
  server.py` → the definitions are gone from server.py (imported instead).
- M5 gate: the new test fails if any of `team_plan/agent_started/
  agent_completed/chart/final` is dropped from the cache-replay path.

## 9. Files touched

- **Create:** `runner/config.py`, `runner/turn/cache.py`,
  `runner/turn/finalize.py` (does not exist yet — Task 6 was deferred),
  `runner/turn/sse.py`, `tests/test_runner/test_conductor_sse.py`.
- **Modify:** `runner/turn/conductor.py` (drop `from server import`; watchdog
  moves in; import constants + `_NODE_TRACE_STORE` from `runner/config.py`; call
  `sse.py`), `server.py` (remove the 9 helper defs + constant/singleton defs;
  import them from `runner/config.py`), `tests/test_server.py` (+ any other
  test) repoint `server._X` references.

## 10. Risks

- **Test repointing breadth** — many `server._X` references (esp.
  `_collect_turn_charts` in `test_server.py`). Mitigation: per-name grep before
  and after; the suite is the backstop; a stale `server._X` fails at collection.
- **`_NODE_TRACE_STORE` move** — its init is self-contained (verified,
  server.py:150–154), so moving it to `runner/config.py` is clean. Confirm
  nothing in server bootstrap must run before it (it depends only on
  `_NODE_TRACE_DB_PATH` + the `NODE_TRACE_DISABLE` env var). The value is a
  singleton imported by value into both modules — same as the other constants,
  so a future `monkeypatch.setattr(server, "_NODE_TRACE_STORE", …)` would not
  reach `conductor` (inert today; note it).
- **M4 `sse.py` emit-state coupling** — the event mapping mutates emit-state
  (`tool_calls`, `call_index_by_id`, `team_plan_emitted`). Pass the state
  explicitly (or keep the mapping a `TurnRunner` method that delegates
  formatting to `sse.py`); do NOT duplicate emit-state. This is the subtlest
  part of Stage 2 — if it gets awkward, keep the mapping inline and treat M4 as
  `_finalize`-only.
- **Constant default drift** — copy the exact env-var names and default values
  from `server.py` into `runner/config.py`; a changed default is a behavior
  change.
