# Turn Runner Pipeline — Design Spec

Date: 2026-07-09
Status: Draft (for review)
Owner: server / runner
Related: `docs/superpowers/specs/2026-07-08-agent-tools-package-design.md` (the tools/ side of the same readability effort)

## 1. Problem

The runtime path of a reviewer turn is hard for a newcomer to follow. Using the
five-layer model from the code walkthrough:

- **L0 — system prompt:** already cohesive (one function per agent in
  `agent_factories/*_agent.py`). Not a problem.
- **L1 — user message:** scattered across `server.py`, `tools/episodic.py`,
  `tools/kb_tools.py`, and `agent_tools/agent_tool.py`.
- **L2 — SDK runner loop / tool outputs:** the loop itself is inside the SDK
  (`agents/run.py`); our part is the drive + SSE mapping in `server.py`.
- **L3 — coherence review + re-dispatch:** in `server.py`.
- **L4 — firewall / safechain:** in `llm/`, cross-cutting.

L1–L4 feel "sparse" because they are **interleaved inside one ~1000-line
function** — `server.py::_run_turn_streamed` — which drives the SDK runner and
hangs input assembly (L1), review (L3), and SSE/event handling off different
points of that drive loop. The runtime turn has no readable spine.

## 2. Goals / Non-goals

**Goals**
- Give the runtime turn a **readable spine**: a `TurnRunner.run()` a newcomer
  can read top-to-bottom, with each phase in a focused, testable unit.
- Make each agent's **L1 input assembly** readable in one place
  (orchestrator: `runner/turn/input_assembly.py`; specialist:
  `assemble_specialist_input` in `specialist_input_tool.py`).
- Group the execution layer under one package: `runner/` (agent-graph
  construction + per-turn drive).

**Non-goals**
- **No behavior changes, no signature changes** to observable behavior. Pure
  structural refactor: code moves; the `nonlocal` closures become `self.`
  methods; logic is unchanged.
- **Do not touch the L2 loop** (SDK-internal) or **L4 firewall** (already
  cohesive and cross-cutting in `llm/`).
- No prompt/skill content changes.

## 3. Target architecture

```
runner/                        ← execution layer
  __init__.py
  orchestrator.py              (moved from orchestrator/orchestrator.py) — class Orchestrator, builds the SDK agent graph
  turn/                        ← per-turn drive pipeline
    __init__.py
    conductor.py               TurnRunner — the spine + the state-coupled stream drive
    screen.py                  L1-pre: chat_agent screen + relevance (pure-ish)
    input_assembly.py          L1: assemble_orchestrator_input(sess, verdict, ctx) -> str
    review.py                  L3: _run_review, _apply_review_directive, gate helpers
    finalize.py                Phase-4 helpers: chart collection / final payload shaping (pure-ish)
tools/agent_tools/
  specialist_input_tool.py     + assemble_specialist_input(...)   ← specialist L1, pulled out of agent_tool._runner
```

The spine — `TurnRunner.run()`:

```python
async def run(self):
    if not await self._screen():   return   # L1-pre: screen + relevance  (turn/screen.py)
    if self._replay_from_cache():  return   # L1.5: exact / near-dup replay (state-coupled → method)
    self._assemble_input()                  # L1  (turn/input_assembly.py)
    await self._run_orchestrator()          # L2 drive + SSE mapping + retry loop (state-coupled → method)
    await self._review_and_redispatch()     # L3  (turn/review.py, via a run_redispatch_pass_fn method)
    await self._finalize()                  # emit final + drain distillers + charts (turn/finalize.py)
```

`server.py::_run_turn_streamed` shrinks to `await TurnRunner(sess, turn_id,
question, started_at).run()`.

**State-coupled vs. pure boundary** (this determines what is a method vs. a
module function):
- **State-coupled → `TurnRunner` methods:** the stream drive + SSE mapping
  (`_run_orchestrator`), the retry loop, the cache-replay (it must re-emit the
  full SSE set), and the redispatch closure. These own/mutate the SSE-emit
  state (`tool_calls`, `call_index_by_id`, `team_plan_emitted`,
  `specialist_errors_emitted`, `final_answer`) and cannot be pure functions.
- **Pure-ish → module functions the methods call:** orchestrator input
  assembly, the review decision helpers, chat-agent screening, chart collection
  / final-payload shaping.

## 4. Component interfaces

- `Orchestrator` (`runner/orchestrator.py`): unchanged, relocated. Builds the
  agent graph.
- `TurnRunner` (`runner/turn/conductor.py`): constructed with
  `(sess, turn_id, question, started_at)`; holds all shared turn state as
  instance attributes; `run()` is the spine. The SSE-emit helpers
  (`_drain_specialist_errors`, `_emit_event`, `_safe_dump`) become methods.
- `assemble_orchestrator_input(sess, verdict, ctx) -> str`
  (`runner/turn/input_assembly.py`): folds `_compose_framed_question`,
  `_format_kb_warmth_hint`, and the episodic wiring
  (`build_records`/`select_episodic`/`render_orchestrator_block`, sets
  `ctx._episodic_records`). Returns the framed question.
- `review.py`: `_run_review`, `_apply_review_directive`,
  `_invalidate_specialist_distillation`, `_is_multi_specialist_turn`,
  `_dispatch_count`, `_bump_dispatch_count`. `_apply_review_directive` already
  takes a `run_redispatch_pass_fn` callback, so the state-coupled redispatch
  closure stays on `TurnRunner` and is passed in — clean extraction.
- `assemble_specialist_input(app_ctx, name, redacted_in, concepts, catalog, data_hints, prior) -> str`
  (`specialist_input_tool.py`): gathers episodic + KB digest + directed
  variables and composes the specialist input, so `agent_tool._runner` calls
  one readable function. Returns the composed input (first-call path); the
  `prior`-transcript branch stays in `_runner`.

## 5. Staging (Approach A, delivered via C first)

Each stage is independently committable and **must leave the full suite green**.

**Stage 1 — relocate + extract clean phases (Approach C; low risk):**
- 1a. `git mv orchestrator/ → runner/orchestrator.py` (+ `runner/__init__.py`);
  update the 8 import sites `from orchestrator.orchestrator import Orchestrator`
  → `from runner.orchestrator import Orchestrator` (`main.py`, `server.py`, 4
  tests, 2 notebooks).
- 1b. Create `runner/turn/input_assembly.py`; move the orchestrator L1 logic;
  `server.py` calls `assemble_orchestrator_input(...)`.
- 1c. Create `runner/turn/review.py`; move the L3 helpers; `server.py` Phase-3.5
  calls them (the `_run_redispatch_pass` closure stays in `server.py` and is
  passed as `run_redispatch_pass_fn`).
- 1d. Add `assemble_specialist_input(...)` to `specialist_input_tool.py`;
  `agent_tool._runner` calls it.
- After Stage 1: `_run_turn_streamed` is smaller but still the driver.

**Stage 2 — introduce the spine (Approach A):**
- 2a. Create `runner/turn/conductor.py::TurnRunner`. Move the turn-handler body
  into phase methods: `_screen` (+ `screen.py`), `_replay_from_cache`,
  `_assemble_input` (calls 1b), `_run_orchestrator` (the Phase-3 stream drive +
  retry loop + SSE mapping), `_review_and_redispatch` (calls 1c),
  `_finalize` (+ `finalize.py`). Shared local state → instance attributes;
  `nonlocal` closures → methods.
- 2b. `server.py::_run_turn_streamed` becomes
  `await TurnRunner(sess, turn_id, question, started_at).run()`.
- The mechanical rule for 2a: **state → `self.`, no logic edits.** Do not
  reorder or "improve" the stream-drive body during the move.

Stopping after Stage 1 is a valid partial win (input assembly + review already
readable). Stage 2 is the higher-risk half and is gated separately.

## 6. SSE invariant (must be preserved)

Every branch that emits a `final` event MUST also emit `team_plan` +
`agent_started` + `agent_completed` + `chart` (cache hit, fallback, retry,
error short-circuit, review re-dispatch). See
`.claude/memory/feedback_alternate_paths_must_replay_full_sse.md`. This is why
cache-replay and the stream drive stay as `TurnRunner` methods sharing the emit
state rather than becoming pure functions. Verification: the existing SSE tests
must stay green, and a manual audit that each `self.sess.emit("final", …)` path
in `conductor.py` is preceded by the full replay set.

## 7. Error handling

Preserve every existing guard verbatim: the orchestrator retry loop
(`_MAX_ORCH_ATTEMPTS`), the plan-timeout watchdog (`_PlanningTimeout` /
`_next_planning_event`), the `_trace_extraction_fallback` /
`_synthesize_fallback_answer` salvage paths, the review block's blanket
try/except that degrades to the phase-1 answer, and the distiller-drain guards.
These move verbatim into methods; none of their logic changes.

## 8. Testing / verification

Run with the **autoAI** interpreter (`~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`);
bare python lacks matplotlib (see `.claude/memory/dev_env_autoai_interpreter.md`).

- Baseline: capture the pass count before each stage (currently **524 passed**).
  Each stage must yield the same count, zero collection errors.
- Grep gate after Stage 1a: `grep -rn "from orchestrator.orchestrator\|orchestrator/orchestrator" . --include=*.py | grep -v brainstorm` → none.
- Standalone import: `python -c "import runner.orchestrator, runner.turn.conductor, runner.turn.input_assembly, runner.turn.review"` succeeds.
- SSE audit (Stage 2): every `final`-emitting path in `conductor.py` also emits
  the full replay set (see §6); existing SSE/alternate-path tests green.
- Test relocation: `tests/test_orchestrator_*` imports repointed to
  `runner.orchestrator`; add/repoint tests for the new `runner/turn/*` module
  functions (`assemble_orchestrator_input`, review helpers,
  `assemble_specialist_input`) — pure functions are newly unit-testable in
  isolation, a side benefit of the extraction.

## 9. Files touched

- **Create:** `runner/__init__.py`, `runner/turn/__init__.py`,
  `runner/turn/conductor.py`, `runner/turn/screen.py`,
  `runner/turn/input_assembly.py`, `runner/turn/review.py`,
  `runner/turn/finalize.py`.
- **Move (git mv):** `orchestrator/orchestrator.py` → `runner/orchestrator.py`
  (remove the now-empty `orchestrator/` package).
- **Modify:** `server.py` (shrinks to the TurnRunner call + moved code removed),
  `tools/agent_tools/agent_tool.py` (`_runner` calls `assemble_specialist_input`),
  `tools/agent_tools/specialist_input_tool.py` (new function), the 8
  `Orchestrator` import sites, and `tests/test_orchestrator_*`.

## 10. Risks

- **`server.py` is the hottest, most SSE-fragile file.** Stage 2 (the
  stream-drive move) is the real risk. Mitigation: mechanical extraction
  (state→self, zero logic edits), the full suite as the gate, and the §6 SSE
  audit. Stage 1 is low-risk and lands the readability of L1/L3 first.
- **Closure→method conversion** (`_drain_specialist_errors`, `_emit_event`,
  `_run_redispatch_pass`, `_next_planning_event` interplay): captured state must
  become instance attributes; a missed capture surfaces as a `NameError`/
  `AttributeError` at collection or in the SSE tests.
- **Two "orchestrator" names** (`agent_factories/orchestrator_agent.py` the
  factory vs `runner/orchestrator.py` the runtime class) — pre-existing; the
  spec leaves both names as-is and documents the distinction.
- **Notebook import drift** (`notebooks/*`): update the two references; they are
  not test-gated, so update them in the same pass to avoid silent staleness.
