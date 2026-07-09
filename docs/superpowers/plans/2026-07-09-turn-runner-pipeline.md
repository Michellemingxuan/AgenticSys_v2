# Turn Runner Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose `server.py::_run_turn_streamed` (~1000 lines) into a readable `TurnRunner` spine under a new `runner/` package, with L1 input assembly and L3 review as focused units.

**Architecture:** A new `runner/` package holds the execution layer: `runner/orchestrator.py` (agent-graph construction, relocated) and `runner/turn/` (per-turn drive). Stage 1 extracts the low-risk pure phases (orchestrator L1, L3 review, specialist L1) into modules that `server.py` calls. Stage 2 introduces `runner/turn/conductor.py::TurnRunner`, moving the turn-handler body into phase methods with a readable `run()` spine.

**Tech Stack:** Python 3.11 (pyenv virtualenv `autoAI`), openai-agents 0.3.3, pytest 8.4.2.

## Global Constraints

- **Behavior-preserving.** No observable behavior or signature changes. Code moves verbatim; `nonlocal` closures become `self.` methods; no logic edits.
- **Interpreter:** run all pytest with `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest`. Bare `python` lacks matplotlib and errors at collection (`.claude/memory/dev_env_autoai_interpreter.md`).
- **Baseline:** `pytest tests/ -q` is **524 passed** today. Every task must leave it at 524 passed, zero collection errors.
- **SSE invariant:** every branch emitting a `final` event must also emit `team_plan` + `agent_started` + `agent_completed` + `chart` (`.claude/memory/feedback_alternate_paths_must_replay_full_sse.md`). Cache-replay and stream-drive stay as `TurnRunner` methods sharing emit-state.
- **Do NOT touch** the SDK L2 loop (`agents/run.py`) or the L4 firewall (`llm/`).
- **Do NOT commit unrelated `brainstorm/*` working-tree changes.** Stage each task's files explicitly.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Stage 1 — Relocate + extract clean phases (Approach C, low risk)

### Task 1: Relocate `orchestrator/` → `runner/orchestrator.py`

**Files:**
- Create: `runner/__init__.py` (empty), `runner/turn/__init__.py` (empty)
- Move: `orchestrator/orchestrator.py` → `runner/orchestrator.py`
- Modify: `main.py:22`, `server.py:70`, `tests/test_orchestrator_run.py:12`, `tests/test_orchestrator_balance_fallback.py:14`, `tests/test_orchestrator_init.py:6`, `notebooks/run_question_suite.py:74`, `tests/test_consistency/run.py:88`, `notebooks/_build_test_chat_mode.py:182`

**Interfaces:**
- Produces: `from runner.orchestrator import Orchestrator` (same class, new path).

- [ ] **Step 1: Capture baseline**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `524 passed, 1 warning in ...`

- [ ] **Step 2: Move the file and create the package**

```bash
mkdir -p runner/turn
touch runner/__init__.py runner/turn/__init__.py
git mv orchestrator/orchestrator.py runner/orchestrator.py
git rm orchestrator/__init__.py
rmdir orchestrator 2>/dev/null || true
```

- [ ] **Step 3: Update all 8 import sites**

In each of `main.py`, `server.py`, `tests/test_orchestrator_run.py`, `tests/test_orchestrator_balance_fallback.py`, `tests/test_orchestrator_init.py`, `notebooks/run_question_suite.py`, `tests/test_consistency/run.py`, and the string literal in `notebooks/_build_test_chat_mode.py:182`, replace:
```python
from orchestrator.orchestrator import Orchestrator
```
with:
```python
from runner.orchestrator import Orchestrator
```

- [ ] **Step 4: Grep gate — no stale references**

Run: `grep -rn "from orchestrator.orchestrator\|orchestrator/orchestrator\|import orchestrator\b" . --include="*.py" | grep -v brainstorm`
Expected: no output (the `agent_factories/orchestrator_agent` imports do NOT match this pattern).

- [ ] **Step 5: Import check + full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import runner.orchestrator; from runner.orchestrator import Orchestrator; print('OK')"`
Expected: `OK`
Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `524 passed`

- [ ] **Step 6: Commit**

```bash
git add runner/ main.py server.py tests/test_orchestrator_run.py tests/test_orchestrator_balance_fallback.py tests/test_orchestrator_init.py tests/test_consistency/run.py notebooks/run_question_suite.py notebooks/_build_test_chat_mode.py
git rm --cached orchestrator/__init__.py 2>/dev/null || true
git commit -m "refactor(runner): relocate orchestrator/ -> runner/orchestrator.py

Establishes the runner/ execution package. Pure move + import updates.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Extract orchestrator L1 → `runner/turn/input_assembly.py`

**Files:**
- Create: `runner/turn/input_assembly.py`, `tests/test_runner/__init__.py`, `tests/test_runner/test_input_assembly.py`
- Modify: `server.py` (remove `_format_kb_warmth_hint` at 564–607, `_compose_framed_question` at 610–612, and the inline assembly block at ~1474–1503; call the new function instead)

**Interfaces:**
- Produces: `assemble_orchestrator_input(sess, verdict, ctx) -> str` — returns the framed question; also sets `ctx._episodic_records`.
- Consumes (unchanged, still imported where used): `tools.episodic.{build_records, select_episodic, render_orchestrator_block, EPISODIC_TURNS}`.

- [ ] **Step 1: Write the new module**

Create `runner/turn/input_assembly.py`. Move the bodies of `_format_kb_warmth_hint` (server.py 564–607) and `_compose_framed_question` (server.py 610–612) verbatim as module-level helpers `_format_kb_warmth_hint` / `_compose_framed_question`, and wrap the current inline assembly (server.py ~1474–1495) as:

```python
"""Layer 1 — orchestrator user-message assembly (episodic + KB warmth + question)."""
from __future__ import annotations

from tools.episodic import (
    EPISODIC_TURNS, build_records, render_orchestrator_block, select_episodic,
)


def _format_kb_warmth_hint(specialist_kb: dict) -> str:
    ...  # verbatim from server.py:564-607


def _compose_framed_question(episodic_block: str, warmth_hint: str, question: str) -> str:
    """Order: episodic (coreference) -> KB warmth (topics) -> question. Skip empties."""
    return "\n\n".join(p for p in (episodic_block, warmth_hint, question) if p)


def assemble_orchestrator_input(sess, verdict, ctx) -> str:
    """Build the orchestrator's framed user message and stash episodic records on ctx.

    Returns the framed question string. Side effect: sets ctx._episodic_records.
    """
    warmth_hint = _format_kb_warmth_hint(sess.specialist_kb)
    try:
        episodic_window = build_records(sess.qa_cache)
        episodic_block = render_orchestrator_block(
            select_episodic(episodic_window, EPISODIC_TURNS))
    except Exception:  # noqa: BLE001 — episodic assembly must never break a turn
        episodic_window, episodic_block = [], ""
        sess.logger.log("episodic_assembly_failed",
                        {"turn_id": getattr(ctx, "_turn_id", None)})
    ctx._episodic_records = episodic_window
    return _compose_framed_question(
        episodic_block, warmth_hint, verdict.redacted_question)
```

Preserve the existing `kb_warmth_hint_emitted` log call from server.py:1476–1484 inside `assemble_orchestrator_input` (move it verbatim, guarded on `if warmth_hint`).

- [ ] **Step 2: Write the failing unit test**

Create `tests/test_runner/test_input_assembly.py`:

```python
import types
from runner.turn.input_assembly import assemble_orchestrator_input, _compose_framed_question


def _fake_sess(qa_cache=None, specialist_kb=None):
    return types.SimpleNamespace(
        qa_cache=qa_cache or {}, specialist_kb=specialist_kb or {},
        logger=types.SimpleNamespace(log=lambda *a, **k: None))


def test_compose_order_and_skip_empties():
    assert _compose_framed_question("", "", "q?") == "q?"
    assert _compose_framed_question("EP", "KB", "q?") == "EP\n\nKB\n\nq?"


def test_assemble_cold_turn_is_bare_question():
    sess = _fake_sess()
    ctx = types.SimpleNamespace(_turn_id="t1")
    verdict = types.SimpleNamespace(redacted_question="why default?")
    out = assemble_orchestrator_input(sess, verdict, ctx)
    assert out == "why default?"
    assert ctx._episodic_records == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_input_assembly.py -v`
Expected: FAIL — `ModuleNotFoundError: runner.turn.input_assembly` (until Step 1's file is saved) or PASS if Step 1 done. If Step 1 is already saved, expect PASS; that is fine for a move.

- [ ] **Step 4: Rewire `server.py`**

Delete `_format_kb_warmth_hint` (564–607) and `_compose_framed_question` (610–612) from `server.py`. Add near the top imports:
```python
from runner.turn.input_assembly import assemble_orchestrator_input
```
Replace the inline block at ~1474–1503 (from `warmth_hint = ...` through `run_input = framed_question`) with:
```python
framed_question = assemble_orchestrator_input(sess, verdict, ctx)
run_input = framed_question
```
After the move, the local `warmth_hint` variable no longer exists in `server.py`. Its one remaining consumer is the log at ~1531 (`"warmth_hint_present": bool(warmth_hint)`). Replace that field with the behavior-equivalent expression (warmth hint is non-empty iff some specialist has KPs):
```python
"warmth_hint_present": bool(sess.specialist_kb and any(sess.specialist_kb.values())),
```

- [ ] **Step 5: Run unit test + full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_input_assembly.py -v`
Expected: PASS
Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `524 passed` (or 526 with the 2 new tests)

- [ ] **Step 6: Commit**

```bash
git add runner/turn/input_assembly.py tests/test_runner/ server.py
git commit -m "refactor(runner): extract orchestrator L1 input assembly

Move _format_kb_warmth_hint, _compose_framed_question, and the episodic
wiring into runner/turn/input_assembly.assemble_orchestrator_input.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Extract L3 review → `runner/turn/review.py`

**Files:**
- Create: `runner/turn/review.py`, `tests/test_runner/test_review.py`
- Modify: `server.py` (remove the six review helpers at 916–1171; import them from the new module)

**Interfaces:**
- Produces (moved verbatim, same signatures): `_is_multi_specialist_turn(ctx) -> bool`, `_dispatch_count(ctx) -> int`, `_bump_dispatch_count(ctx) -> int`, `async _run_review(sess, ctx, question, specialist_outputs)`, `async _invalidate_specialist_distillation(ctx, specialist, turn_id) -> dict`, `async _apply_review_directive(*, sess, ctx, framed_question, tool_calls, streamed, turn_id, run_redispatch_pass_fn) -> tuple`, plus the module constants `_AUX_REVIEW_TOOLS`.
- Consumes: `run_redispatch_pass_fn` (a callback) — the state-coupled `_run_redispatch_pass` closure stays in `server.py` and is passed in unchanged.

- [ ] **Step 1: Create `runner/turn/review.py`**

Move `_AUX_REVIEW_TOOLS` (server.py:916) and the six functions at server.py 919–1171 **verbatim** into `runner/turn/review.py`. Carry their imports: `asyncio`, `json`, `from llm.firewall_stack import redact_payload`, and the local import `from agent_factories.general_specialist import build_general_specialist` (stays inside `_run_review`). Also move `_ORCH_PLAN_TIMEOUT_S` usage — keep it imported from `server` is a cycle; instead move the `_ORCH_PLAN_TIMEOUT_S` constant definition into `review.py` if it is only used by `_run_review`, else import it from a config module. Verify with: `grep -n "_ORCH_PLAN_TIMEOUT_S" server.py` — if used elsewhere, define it in `review.py` reading the same env var: `_ORCH_PLAN_TIMEOUT_S = float(os.environ.get("ORCH_PLAN_TIMEOUT_S", "<current default>"))` (copy the exact default from server.py).

- [ ] **Step 2: Write the failing unit test**

Create `tests/test_runner/test_review.py`:

```python
import types
from runner.turn.review import _is_multi_specialist_turn, _dispatch_count, _bump_dispatch_count


def test_multi_specialist_gate():
    ctx = types.SimpleNamespace(_domain_specialists_called={"a"})
    assert _is_multi_specialist_turn(ctx) is False
    ctx._domain_specialists_called = {"a", "b"}
    assert _is_multi_specialist_turn(ctx) is True


def test_dispatch_count_clamped_at_2():
    ctx = types.SimpleNamespace()
    assert _dispatch_count(ctx) == 0
    _bump_dispatch_count(ctx); _bump_dispatch_count(ctx); _bump_dispatch_count(ctx)
    assert _dispatch_count(ctx) == 2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_runner/test_review.py -v`
Expected: FAIL — `ModuleNotFoundError` until Step 1 saved, then PASS.

- [ ] **Step 4: Rewire `server.py`**

Delete the six functions + `_AUX_REVIEW_TOOLS` from `server.py`. Add import:
```python
from runner.turn.review import (
    _apply_review_directive, _dispatch_count, _is_multi_specialist_turn,
)
```
(Import only the names `server.py` still references directly — `_is_multi_specialist_turn`, `_dispatch_count`, `_apply_review_directive`, and `_bump_dispatch_count` if used at 1981. Verify usages: `grep -n "_run_review\|_apply_review_directive\|_is_multi_specialist_turn\|_dispatch_count\|_bump_dispatch_count\|_invalidate_specialist_distillation" server.py`.) The `_run_redispatch_pass` closure at ~1984 stays in `server.py` and is passed to `_apply_review_directive(run_redispatch_pass_fn=_run_redispatch_pass)` exactly as today.

- [ ] **Step 5: Grep gate + full suite**

Run: `grep -n "async def _run_review\|def _apply_review_directive\|def _is_multi_specialist_turn" server.py`
Expected: no output (all moved).
Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `524 passed` (+ new tests)

- [ ] **Step 6: Commit**

```bash
git add runner/turn/review.py tests/test_runner/test_review.py server.py
git commit -m "refactor(runner): extract L3 coherence-review helpers to runner/turn/review.py

Move _run_review / _apply_review_directive / gate helpers verbatim. The
state-coupled _run_redispatch_pass closure stays in server.py and is
passed via run_redispatch_pass_fn.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Specialist L1 assembler → `assemble_specialist_input` in `specialist_input_tool.py`

**Files:**
- Modify: `tools/agent_tools/specialist_input_tool.py` (add the function), `tools/agent_tools/agent_tool.py` (`_runner` calls it)
- Test: `tests/test_tools/test_agent_tools/test_specialist_input_tool.py` (create)

**Interfaces:**
- Produces: `assemble_specialist_input(app_ctx, name, redacted_in, concepts, catalog, data_hints, logger) -> tuple[str, int]` — returns `(contextual_in, kb_digest_n_kps)`. Called only on the first-call path (when `prior` is falsy).
- Consumes: `tools.kb_tools.{_format_kb_digest, _active_kps}`, `tools.episodic.{render_specialist_block, select_specialist_episodic, EPISODIC_TURNS}`, `catalog.variables_for_concepts`, and the local `_compose_specialist_input` / `_render_directed_variables`.

- [ ] **Step 1: Add the function to `specialist_input_tool.py`**

Move the block-gathering logic from `agent_tool._runner` (the `if not prior:` body that builds `kb_digest`, `_episodic_block`, `_directed_block` and calls `_compose_specialist_input`) into:

```python
def assemble_specialist_input(app_ctx, name, redacted_in, concepts, catalog,
                              data_hints, logger) -> tuple[str, int]:
    """Layer 5 — build a specialist's first-call input: episodic + KB digest +
    directed variables + sub-question. Returns (contextual_in, kb_digest_n_kps)."""
    from tools.kb_tools import _active_kps, _format_kb_digest
    from tools.episodic import (
        EPISODIC_TURNS, render_specialist_block, select_specialist_episodic,
    )
    kb_digest, kps_for_name = "", []
    kb_obj = getattr(app_ctx, "_specialist_kb", None)
    if name != "report_agent" and isinstance(kb_obj, dict):
        kps_for_name = kb_obj.get(name, [])
        kb_digest = _format_kb_digest(kps_for_name, full_kb=kb_obj, self_name=name)
    try:
        _recs = getattr(app_ctx, "_episodic_records", None) or []
        _episodic_block = render_specialist_block(
            select_specialist_episodic(_recs, name, EPISODIC_TURNS))
    except Exception:  # noqa: BLE001
        _episodic_block = ""
    _directed_block = ""
    if concepts and catalog is not None and data_hints:
        try:
            _vars = catalog.variables_for_concepts(data_hints, concepts)
            _directed_block = _render_directed_variables(_vars)
        except Exception:  # noqa: BLE001
            _directed_block = ""
    contextual_in = _compose_specialist_input(
        _episodic_block, kb_digest, redacted_in, _directed_block)
    return contextual_in, (len(_active_kps(kps_for_name)) if kb_digest else 0)
```

Preserve the existing `directed_variables_injected` / `episodic_specialist_assembly_failed` log calls by passing `logger` and emitting them inside the `try` blocks verbatim.

- [ ] **Step 2: Write the failing unit test**

Create `tests/test_tools/test_agent_tools/test_specialist_input_tool.py`:

```python
import types
from tools.agent_tools.specialist_input_tool import assemble_specialist_input


def test_cold_specialist_input_is_bare_question():
    ctx = types.SimpleNamespace(_specialist_kb={}, _episodic_records=[])
    out, n = assemble_specialist_input(
        ctx, "modeling", "which scores breached?", concepts=None,
        catalog=None, data_hints=["model_scores"], logger=None)
    assert out == "which scores breached?"
    assert n == 0
```

- [ ] **Step 3: Run to verify fail → then pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_agent_tools/test_specialist_input_tool.py -v`
Expected: PASS once Step 1 saved.

- [ ] **Step 4: Rewire `agent_tool._runner`**

Replace the inline `if not prior:` block in `agent_tool.py::_runner` with:
```python
contextual_in = redacted_in
kb_digest_n_kps = 0
if not prior:
    contextual_in, kb_digest_n_kps = assemble_specialist_input(
        app_ctx, name, redacted_in, concepts, catalog, data_hints, logger)
```
Add `from tools.agent_tools.specialist_input_tool import assemble_specialist_input` to `agent_tool.py`'s imports. Leave the `report_agent` file-list injection and the `if prior:` transcript branch exactly as they are.

- [ ] **Step 5: Full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `524 passed` (+ new tests). The existing `test_agent_tool.py` KB-digest tests (`test_agent_tool_prepends_kb_digest_when_no_intra_turn_history`, `..._skips_kb_digest_on_intra_turn_followup`) guard this behavior — they must stay green.

- [ ] **Step 6: Commit**

```bash
git add tools/agent_tools/specialist_input_tool.py tools/agent_tools/agent_tool.py tests/test_tools/test_agent_tools/test_specialist_input_tool.py
git commit -m "refactor(agent_tools): extract assemble_specialist_input (specialist L1)

Pull the episodic + KB-digest + directed-variable gathering out of
agent_tool._runner into a single readable specialist_input_tool function.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Stage 2 — Introduce the spine (Approach A, higher risk)

### Task 5: `TurnRunner` class — `runner/turn/conductor.py`

**Files:**
- Create: `runner/turn/conductor.py`
- Modify: `server.py` (`_run_turn_streamed` body → `TurnRunner(...).run()`)

**Interfaces:**
- Produces: `class TurnRunner` with `__init__(self, sess, turn_id, question, started_at=None)` and `async def run(self)`.
- Consumes: `assemble_orchestrator_input` (Task 2), the `review.py` helpers (Task 3), and everything `_run_turn_streamed` currently imports.

**Mechanical rule for this whole task: state → `self.`, closures → methods, ZERO logic edits.** Do not reorder or "clean up" the stream-drive body while moving it.

- [ ] **Step 1: Scaffold the class with all shared state as attributes**

Create `runner/turn/conductor.py`. Enumerate every local in `_run_turn_streamed` that is read across phase boundaries and declare it in `__init__` (verify the list against `server.py` before writing):

```python
class TurnRunner:
    """The runtime spine for one reviewer turn. Each phase is a method;
    run() is the readable order. State the phases share lives on self."""

    def __init__(self, sess, turn_id, question, started_at=None):
        self.sess = sess
        self.turn_id = turn_id
        self.question = question
        self.started_at = started_at
        # filled by phases:
        self.verdict = None
        self.ctx = None
        self.framed_question = None
        self.run_input = None
        self.streamed = None
        self.final_answer = None
        self.review_flags = []
        # SSE-emit state (Phase 3):
        self.call_index_by_id = {}
        self.tool_calls = []
        self.started_at_by_call = {}
        self.team_plan_emitted = False
        self.first_tool_call_logged = False
        self.specialist_errors_emitted = 0
```

- [ ] **Step 2: Move Phase 1/1.5 into `_screen` / `_replay_from_cache`**

Move the screen+relevance block (server.py 1211–1316) into `async def _screen(self) -> bool` (return `False` when the turn short-circuits, e.g. screen timeout / rejected). Move the cache-lookup block (1317–1422) into `def _replay_from_cache(self) -> bool` (return `True` when a cached answer was replayed). Convert every local to `self.`. **The cache-replay path must emit the full SSE set** — move it verbatim, do not drop any emit.

- [ ] **Step 3: Move Phase 2 into `_assemble_input`**

Move the ctx construction + `run_input` build (server.py ~1423–1517) into `def _assemble_input(self)`, calling `assemble_orchestrator_input(self.sess, self.verdict, self.ctx)` (Task 2 already did the extraction; here just relocate the surrounding ctx setup).

- [ ] **Step 4: Move Phase 3 into `_run_orchestrator`**

Move the entire stream-drive + retry loop (server.py 1518–1963) into `async def _run_orchestrator(self)`. Convert the closures `_drain_specialist_errors`, `_safe_dump`, `_emit_event` (defined earlier at ~1447) and the `nonlocal` vars into methods / `self.` attributes. Keep `_next_planning_event` / `_PlanningTimeout` as module-level helpers imported into `conductor.py` (move them there from `server.py` 640–664).

- [ ] **Step 5: Move Phase 3.5 into `_review_and_redispatch`**

Move server.py 1964–2079 into `async def _review_and_redispatch(self)`. The `_run_redispatch_pass` closure becomes `async def _run_redispatch_pass(self, resume_input)`; pass `self._run_redispatch_pass` as `run_redispatch_pass_fn` to `_apply_review_directive`.

- [ ] **Step 6: Move Phase 4 into `_finalize`**

Move server.py 2081–~2280 (emit final + drain distillers + charts) into `async def _finalize(self)`.

- [ ] **Step 7: Write the `run()` spine**

```python
    async def run(self):
        if not await self._screen():   return
        if self._replay_from_cache():  return
        self._assemble_input()
        await self._run_orchestrator()
        await self._review_and_redispatch()
        await self._finalize()
```

- [ ] **Step 8: Replace `server.py::_run_turn_streamed` body**

```python
async def _run_turn_streamed(sess, turn_id, question, started_at=None):
    from runner.turn.conductor import TurnRunner
    await TurnRunner(sess, turn_id, question, started_at).run()
```
Remove the now-dead helpers left behind in `server.py` (the moved closures/constants). Verify none are still referenced: `grep -n "_drain_specialist_errors\|_next_planning_event\|_PlanningTimeout" server.py`.

- [ ] **Step 9: Full suite + SSE audit**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `524 passed` (+ Stage-1 new tests)
SSE audit: `grep -n 'emit("final"\|emit(\"final\"' runner/turn/conductor.py` — for each hit, confirm the same code path also emits `team_plan`, `agent_started`, `agent_completed`, `chart` (compare against the pre-refactor `server.py` paths). Confirm the existing alternate-path SSE tests pass (e.g. `grep -rl "team_plan" tests/` then run those files).

- [ ] **Step 10: Commit**

```bash
git add runner/turn/conductor.py server.py
git commit -m "refactor(runner): introduce TurnRunner spine for the runtime turn

Move server.py::_run_turn_streamed body into runner/turn/conductor.py as
phase methods with a readable run() spine. Mechanical: state->self,
closures->methods, no logic changes. Behavior-preserving (524 passed).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Extract pure phase helpers `screen.py` / `finalize.py` (polish, optional)

**Files:**
- Create: `runner/turn/screen.py`, `runner/turn/finalize.py`
- Modify: `runner/turn/conductor.py` (methods delegate to the pure helpers)

**Interfaces:**
- Produces: `runner/turn/screen.py::screen_question(sess, question, prior_questions) -> verdict` (the pure chat-agent call + timeout handling, no SSE); `runner/turn/finalize.py::collect_turn_charts(ctx) -> list` and any pure final-payload shaping. SSE-emitting parts stay in `conductor.py`.

- [ ] **Step 1: Move the pure screen call into `screen.py`**

Extract only the non-SSE part of `_screen` (the `sess.chat_agent.screen(...)` call wrapped in the timeout) into `screen_question(...)`; `TurnRunner._screen` calls it and keeps the SSE/log emits.

- [ ] **Step 2: Move chart collection into `finalize.py`**

Extract the pure chart-collection logic (`_collect_turn_charts` or equivalent) into `finalize.py`; `_finalize` calls it and keeps the emits.

- [ ] **Step 3: Full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q 2>&1 | tail -1`
Expected: `524 passed` (+ new tests)

- [ ] **Step 4: Commit**

```bash
git add runner/turn/screen.py runner/turn/finalize.py runner/turn/conductor.py
git commit -m "refactor(runner): extract pure screen/finalize helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Task 6 is optional polish; Stage 1 + Task 5 already deliver the readable spine.

---

## Verification summary (run after each stage)

- `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q` → **524 passed** (+ new unit tests), zero collection errors.
- Grep gates as specified per task.
- SSE audit (Task 5): every `final`-emitting path in `conductor.py` also emits the full replay set.
- Do NOT push or commit `brainstorm/*`.
