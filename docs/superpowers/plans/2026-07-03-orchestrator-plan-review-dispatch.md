# Orchestrator Plan–Review Dispatch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the orchestrator act as a manager that plans dispatch by judgment, runs a server-enforced coherence review, and re-dispatches (or early-releases) so causal multi-specialist answers are coherent, not two disconnected halves.

**Architecture:** Up-front dispatch shape is chosen by the orchestrator prompt (parallel / collapse / sequential). A server-driven phased run interposes a review step between dispatch and synthesis on multi-specialist turns: (1) dispatch+gather, (2) server-invoked `general_specialist` review (un-skippable), (3) synthesize. The reviewer only advises (emits a `ReviewDirective`); the orchestrator acts (re-dispatch ≤ once, or early-release with a double-gate + straggler cancellation). Early-release cancellation is clean because specialist LLM calls now use cancellable `ainvoke`.

**Tech Stack:** Python 3.11, `openai-agents` SDK (`Runner.run_streamed`, RunHooks), Pydantic v2, pytest / pytest-asyncio. Prod backend = safechain (non-streaming under a synthetic stream); dev = OpenAI.

## Global Constraints

- **Caps:** ≤ 2 dispatch rounds per turn (initial + at most one re-dispatch); a specialist is invoked ≤ 2× per turn (carries ≤ 1 prior exchange, matching `_SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES = 2`).
- **Reviewer runs only on multi-specialist turns** (≥ 2 specialists dispatched). Single-specialist turns keep the current single-run path unchanged (zero added latency).
- **Reviewer is advisory only** — never dispatches, never produces domain analysis, keeps verification-only tools.
- **Parallel dispatch stays the default shape.**
- **Reviewer failure/timeout degrades gracefully** — synthesize from existing outputs; never block the turn on the reviewer.
- **Do not auto-commit** unless the developer running the plan chooses to; commit steps are included but the human decides when to push.
- **safechain parity:** any change to the specialist/orchestrator call path must work under both the OpenAI (`firewall_client.py`) and safechain (`safechain_client.py`) backends.

---

## File Structure

- `models/types.py` — add `ReviewDirective`; extend `ReviewReport` with `directive`. (Modify)
- `agent_factories/general_specialist.py` — reviewer stays; instructions gain coherence + qualified-release + directive emission. (Modify)
- `skills/workflow/comparison.md` — reviewer skill body: coherence/alignment review, qualified-release, directive schema. (Modify)
- `skills/workflow/team_construction.md` — add "Dispatch shape" section; remove row-31 `NOT TSR/CDSS`. (Modify)
- `agent_factories/orchestrator_agent.py` — VP framing; drop forced single-response parallel mandate. (Modify)
- `server.py` — phased run (dispatch/gather → review → synthesize), multi-specialist gating, dispatch/specialist caps, early-release via hooks, straggler cancellation. (Modify)
- `tests/test_models/test_review_directive.py` — schema tests. (Create)
- `tests/test_agent_factories/test_general_specialist_review.py` — reviewer directive tests. (Create)
- `tests/test_server/test_plan_review_dispatch.py` — phasing, caps, early-release, integration. (Create)

---

## Task 1: `ReviewDirective` schema

**Files:**
- Modify: `models/types.py` (near `class ReviewReport` ~line 168)
- Test: `tests/test_models/test_review_directive.py`

**Interfaces:**
- Produces: `ReviewDirective` Pydantic model with fields `kind: Literal["coherent","needs_redispatch","qualified_release"]`, `specialist: str | None`, `anchor: str | None`, `why: str | None`, `release_specialist: str | None`; and `ReviewReport.directive: ReviewDirective | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models/test_review_directive.py
import pytest
from pydantic import ValidationError
from models.types import ReviewDirective, ReviewReport


def test_coherent_directive_minimal():
    d = ReviewDirective(kind="coherent")
    assert d.kind == "coherent"
    assert d.specialist is None and d.release_specialist is None


def test_needs_redispatch_directive():
    d = ReviewDirective(kind="needs_redispatch", specialist="modeling",
                        anchor="2025-05", why="drivers not anchored to the spike")
    assert d.specialist == "modeling"
    assert d.anchor == "2025-05"


def test_qualified_release_directive():
    d = ReviewDirective(kind="qualified_release", release_specialist="spend_payments")
    assert d.release_specialist == "spend_payments"


def test_review_report_carries_directive_optional():
    r = ReviewReport()  # existing default-constructible report
    assert r.directive is None
    r2 = ReviewReport(directive=ReviewDirective(kind="coherent"))
    assert r2.directive.kind == "coherent"


def test_invalid_kind_rejected():
    with pytest.raises(ValidationError):
        ReviewDirective(kind="ship_it")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_models/test_review_directive.py -q`
Expected: FAIL with `ImportError: cannot import name 'ReviewDirective'`.

- [ ] **Step 3: Write minimal implementation**

Add to `models/types.py` (after the `Conflict` class, before/around `ReviewReport`):

```python
class ReviewDirective(BaseModel):
    """The reviewer's advisory output. The orchestrator ACTS on it; the
    reviewer never dispatches. See docs/superpowers/specs/
    2026-07-03-orchestrator-plan-review-dispatch-design.md."""
    kind: Literal["coherent", "needs_redispatch", "qualified_release"] = "coherent"
    # needs_redispatch:
    specialist: str | None = None          # who to re-run
    anchor: str | None = None              # window/event to anchor to, e.g. "2025-05"
    why: str | None = None                 # one line; feeds the sub-question + flags
    # qualified_release:
    release_specialist: str | None = None  # whose output is sufficient to ship
```

Then add one field to the existing `ReviewReport` model:

```python
    directive: ReviewDirective | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_models/test_review_directive.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add models/types.py tests/test_models/test_review_directive.py
git commit -m "feat(types): add ReviewDirective for plan-review dispatch"
```

---

## Task 2: Reviewer emits coherence / qualified-release directives

**Files:**
- Modify: `skills/workflow/comparison.md` (reviewer skill body)
- Modify: `agent_factories/general_specialist.py` (docstring/comment only; output type already `ReviewReport`)
- Test: `tests/test_agent_factories/test_general_specialist_review.py`

**Interfaces:**
- Consumes: `ReviewReport`, `ReviewDirective` (Task 1); `build_general_specialist(model)`.
- Produces: a reviewer whose `ReviewReport.directive` is set to `needs_redispatch` for a temporally-misaligned pair, `coherent` for an aligned pair, `qualified_release` when one output fully answers. (Behavior is prompt-driven; tests use a stub model that returns canned `ReviewReport` JSON to lock the wiring, not the LLM's judgment.)

- [ ] **Step 1: Write the failing test** (wiring + schema round-trip via a stub model)

```python
# tests/test_agent_factories/test_general_specialist_review.py
import json
import pytest
from agent_factories.general_specialist import build_general_specialist
from models.types import ReviewReport, ReviewDirective


def test_review_report_parses_directive_from_model_json():
    # The reviewer's output_type is ReviewReport; assert a directive-bearing
    # ReviewReport round-trips through model_validate (what the SDK does).
    payload = {
        "resolved": [], "open_conflicts": [],
        "directive": {"kind": "needs_redispatch", "specialist": "modeling",
                      "anchor": "2025-05", "why": "drivers not anchored to spike"},
    }
    report = ReviewReport.model_validate(payload)
    assert report.directive.kind == "needs_redispatch"
    assert report.directive.specialist == "modeling"


def test_build_general_specialist_output_type_is_review_report():
    class _M:  # minimal stand-in; agent construction must not call the model
        pass
    agent = build_general_specialist(model=_M())
    # AgentOutputSchema wraps ReviewReport
    assert agent.output_type.output_type is ReviewReport
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_factories/test_general_specialist_review.py -q`
Expected: FAIL (`report.directive` is `None` only if Task 1 not applied — else the second test fails on `output_type.output_type` attribute name).
Note: if the attribute path differs, inspect `agent.output_type` and adjust the assertion to the real accessor before proceeding.

- [ ] **Step 3: Write the reviewer skill guidance**

In `skills/workflow/comparison.md`, add a section (verbatim):

```markdown
## Coherence review + directive (plan-review dispatch)

You are review-only. You do NOT dispatch, do NOT run domain analysis, do NOT
substitute for a specialist. Your job is to judge the specialists' outputs and
emit ONE `directive` in your `ReviewReport`:

- `kind: "coherent"` — the outputs cohere and each explanation/driver analysis is
  anchored to the event it explains. Nothing to do.
- `kind: "needs_redispatch"` — a specialist's analysis is NOT anchored to the
  event it is meant to explain (e.g. spend spiked in 2025-05 but the driver
  analysis is anchored to 2024-09). Set `specialist` = who to re-run, `anchor` =
  the correct window/event (e.g. "2025-05"), `why` = one line.
- `kind: "qualified_release"` — a SINGLE specialist output already fully and
  coherently answers the question (an over-reaching answer). Set
  `release_specialist` = that specialist. Use ONLY when it is genuinely complete;
  a partial answer is NOT qualified.

Anchor check: for causal questions ("what drives / caused X"), the driver
analysis's time window MUST match the window of X established by the other
specialist. If it doesn't, emit `needs_redispatch`.

You may use your verification tools (`aggregate_column`, `get_table_schema`) only
to CHECK a date/anchor, never to introduce new analysis.
```

Update the module docstring in `agent_factories/general_specialist.py` to note the reviewer now emits a `ReviewDirective` (advisory; the orchestrator acts).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_factories/test_general_specialist_review.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/workflow/comparison.md agent_factories/general_specialist.py tests/test_agent_factories/test_general_specialist_review.py
git commit -m "feat(reviewer): coherence + qualified-release directives (advisory)"
```

---

## Task 3: Dispatch-shape guidance + remove modeling TSR/CDSS restriction

**Files:**
- Modify: `skills/workflow/team_construction.md`
- Test: `tests/test_server/test_plan_review_dispatch.py::test_team_construction_skill_content`

**Interfaces:**
- Produces: skill text the orchestrator loads; asserted by content tests (no runtime behavior to unit-test at the skill layer).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server/test_plan_review_dispatch.py
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "skills" / "workflow"


def test_team_construction_skill_content():
    body = (SKILLS / "team_construction.md").read_text(encoding="utf-8")
    # dispatch-shape guidance present
    assert "Dispatch shape" in body
    for shape in ("parallel", "collapse", "sequential"):
        assert shape in body.lower()
    # row-31 restriction removed
    assert "NOT TSR/CDSS" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py::test_team_construction_skill_content -q`
Expected: FAIL (`"Dispatch shape" not in body` and/or `"NOT TSR/CDSS"` still present).

- [ ] **Step 3: Edit the skill**

In `skills/workflow/team_construction.md`:
(a) In the row-31 cell of the "Cross-domain topics" table, delete the clause
`ML-derived spend features only (out-of-pattern, concentration risk-rate, spend divergence — NOT output scores like TSR/CDSS)` and replace with:
`ML score response to the spending — out-of-pattern / concentration / divergence features AND the output scores they move (CDSS/TSR gate approval, so they are in-scope for spend-driver questions).`
(b) Add a new section:

```markdown
## Dispatch shape (parallel-first; the VP's judgment)

You are the manager. Get a sharp, coherent answer in the fewest rounds. Pick a
shape per turn — you are NOT limited to firing everyone at once:

- **parallel** (DEFAULT) — independent sub-questions; emit them together.
- **collapse** — when a question is causally dependent ("what drives X") and ONE
  specialist can self-anchor by cross-querying, hand it the whole chain:
  *"modeling: find the spend-spike month from spends_data yourself, then analyze
  the model-score drivers around that month."* One specialist, no extra round.
- **sequential** — when the anchor needs another specialist's DEEP analysis
  first: dispatch the anchor specialist, read its result, then dispatch the
  dependent with the anchor threaded into its sub-question.

Specialists can query ANY table, so prefer parallel or collapse; use sequential
only when the anchor is itself heavy. Add a round only when a dependency needs it.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py::test_team_construction_skill_content -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/workflow/team_construction.md tests/test_server/test_plan_review_dispatch.py
git commit -m "feat(routing): dispatch-shape guidance; unblock modeling CDSS/TSR"
```

---

## Task 4: Orchestrator VP framing (drop forced single-response parallel)

**Files:**
- Modify: `agent_factories/orchestrator_agent.py` (~line 101)
- Test: `tests/test_server/test_plan_review_dispatch.py::test_orchestrator_instructions_vp_framing`

**Interfaces:**
- Produces: orchestrator agent instructions that no longer force all-parallel-in-one-response and describe the VP role. Asserted by string content on the built agent's `instructions`.

- [ ] **Step 1: Write the failing test**

```python
def test_orchestrator_instructions_vp_framing():
    import agent_factories.orchestrator_agent as oa
    text = oa._read_instruction_text() if hasattr(oa, "_read_instruction_text") else None
    # If instructions are assembled inline, assert on the module source instead:
    import inspect
    src = text or inspect.getsource(oa)
    assert "SINGLE response" not in src  # forced-parallel mandate removed
    assert "manager" in src.lower() or "dispatch shape" in src.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py::test_orchestrator_instructions_vp_framing -q`
Expected: FAIL (`"SINGLE response"` still present).

- [ ] **Step 3: Edit the instruction**

In `agent_factories/orchestrator_agent.py` around line 101, replace the line that says specialists must be emitted "in a SINGLE response so they run in parallel" with:

```python
            "You are the manager. Dispatch the team by judgment (see the "
            "team_construction Dispatch-shape guidance): prefer parallel for "
            "independent sub-questions; collapse a causal dependency into one "
            "cross-querying specialist when possible; sequence only when an "
            "anchor needs another specialist's deep analysis first. Emit "
            "independent specialist calls together so they run in parallel."
```

Keep `parallel_tool_calls=True` (line ~224) — parallel remains the default and common case.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py::test_orchestrator_instructions_vp_framing -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_factories/orchestrator_agent.py tests/test_server/test_plan_review_dispatch.py
git commit -m "feat(orchestrator): VP dispatch framing; drop forced parallel mandate"
```

---

## Task 5 (SPIKE): Resolve the phased-run stop/resume mechanic

This is the one unresolved mechanic (spec §10.1). It must be prototyped before Tasks 6–7 can have exact code. Timebox: ~half a day. Deliverable: a short decision note + a working spike, not production code.

**Files:**
- Create: `docs/superpowers/specs/2026-07-03-phased-run-spike.md` (findings)
- Scratch prototype under the scratchpad dir (not committed to `server.py` yet)

- [ ] **Step 1: Determine how to end the dispatch phase.** Investigate, in `openai-agents`, the cleanest way to run the orchestrator so it STOPS after gathering specialist outputs and BEFORE synthesizing. Evaluate, in order:
  1. `tool_use_behavior` / a sentinel "dispatch_done" tool the orchestrator calls when done dispatching (stop-on-tool).
  2. Running dispatch and synthesis as two separate agents (a dispatcher agent whose output is the gathered set; a synthesizer agent seeded with outputs + directive).
  3. Splitting one agent into two `Runner.run` calls with `result.to_input_list()` carrying state across the boundary.
- [ ] **Step 2: Confirm it works on BOTH backends.** Prototype the chosen approach with the dev OpenAI backend AND validate the control-flow assumptions against safechain's synthetic `_FakeAsyncStream` (recall: non-streaming under the stream; tool-lifecycle hooks DO fire — `trace_hooks` prove it). Verify: (a) dispatch phase ends deterministically, (b) specialist outputs are recoverable in server code, (c) a second run can be seeded with an injected reviewer directive message.
- [ ] **Step 3: Confirm hook-driven early cancellation.** Verify that a RunHook `on_tool_end` (per specialist) fires as each specialist returns, and that pending specialist `asyncio.Task`s can be cancelled from server code (they can now — `ainvoke`).
- [ ] **Step 4: Write the decision note** `docs/superpowers/specs/2026-07-03-phased-run-spike.md` capturing: chosen mechanism, the exact SDK calls, how outputs cross the phase boundary, how the directive is injected, and the cancellation handle. Tasks 6–7 depend on this.
- [ ] **Step 5: Commit the note**

```bash
git add docs/superpowers/specs/2026-07-03-phased-run-spike.md
git commit -m "docs(spike): phased-run stop/resume mechanic for plan-review dispatch"
```

---

## Task 6: Server phased run — dispatch → server review → synthesize (+ caps)

Depends on Task 5's decision note for exact SDK calls. Interfaces below are fixed regardless of the spike outcome; fill the bodies per the note.

**Files:**
- Modify: `server.py` (the turn runner around lines 1287–1620)
- Test: `tests/test_server/test_plan_review_dispatch.py`

**Interfaces:**
- Consumes: `ReviewReport` / `ReviewDirective` (Task 1); `build_general_specialist` (Task 2); the dispatch/gather phase from the spike (Task 5).
- Produces (server-internal helpers, exact signatures for Task 7 to reuse):
  - `def _is_multi_specialist_turn(ctx) -> bool` — True iff `len(ctx._domain_specialists_called) >= 2`.
  - `async def _run_review(sess, ctx, question, specialist_outputs: dict) -> ReviewReport | None` — invokes `general_specialist` in server code; returns None on failure (graceful).
  - `def _dispatch_count(ctx) -> int` and a bump helper; enforce the ≤ 2 cap.

- [ ] **Step 1: Write the failing test** (gating + cap, using a fake reviewer + fake orchestrator phases injected via monkeypatch)

```python
import pytest
import server


@pytest.mark.asyncio
async def test_review_skipped_for_single_specialist(monkeypatch):
    calls = {"review": 0}
    async def fake_review(*a, **k):
        calls["review"] += 1
        return None
    monkeypatch.setattr(server, "_run_review", fake_review)
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments"}  # only 1
    assert server._is_multi_specialist_turn(ctx) is False


@pytest.mark.asyncio
async def test_review_runs_for_multi_specialist(monkeypatch):
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments", "modeling"}
    assert server._is_multi_specialist_turn(ctx) is True


def test_dispatch_cap_blocks_third_dispatch():
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._dispatch_count = 2
    assert server._dispatch_count(ctx) == 2
    # a re-dispatch request at count 2 must be refused by the caller (asserted
    # in the integration test); here we lock the accessor.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py -q`
Expected: FAIL (`_is_multi_specialist_turn` / `_dispatch_count` not defined).

- [ ] **Step 3: Implement the helpers + phasing.** In `server.py`:
  - Add `_is_multi_specialist_turn`, `_run_review`, `_dispatch_count`/bump per the Interfaces block. `_run_review` builds/uses the shared `general_specialist`, passes `{question, specialist_outputs}`, wraps in `asyncio.wait_for` (reuse an existing timeout budget), returns `None` on any exception (log `review_failed`).
  - Restructure the turn runner (per Task 5's note) so that on a multi-specialist turn: run dispatch/gather → call `_run_review` → if `directive.kind == "needs_redispatch"` and `_dispatch_count < 2`, re-dispatch the named specialist with `anchor`+`why` threaded into the sub-question (reuse the existing `corrected_specialist` re-invocation path around server.py:726) and bump the count → then synthesize. On `coherent` or cap reached → synthesize (add a flag if capped-with-residual).
  - Single-specialist turns: keep the existing single-run path untouched.
  - Add `AppContext._dispatch_count: int = 0` (default) to `agent_factories/app_context.py`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py -q`
Expected: PASS (gating + cap accessors).

- [ ] **Step 5: Commit**

```bash
git add server.py agent_factories/app_context.py tests/test_server/test_plan_review_dispatch.py
git commit -m "feat(server): phased run with server-enforced coherence review + caps"
```

---

## Task 7: Early qualified-release + straggler cancellation (double-gate)

**Files:**
- Modify: `server.py` (the dispatch/gather phase; RunHooks)
- Test: `tests/test_server/test_plan_review_dispatch.py`

**Interfaces:**
- Consumes: `_run_review`, `_is_multi_specialist_turn` (Task 6); the per-specialist `on_tool_end` hook + cancellation handle (Task 5).
- Produces: `async def _early_release_check(sess, ctx, question, returned_output, pending_tasks) -> bool` — runs the reviewer on the single returned output; if `qualified_release`, the ORCHESTRATOR (server, on its behalf) re-reviews; only if BOTH concur, cancels `pending_tasks` and returns True (release). Specialist ≤ 2× cap enforced.

- [ ] **Step 1: Write the failing test** (double-gate + cancellation + no-orphan)

```python
import asyncio
import pytest
import server


@pytest.mark.asyncio
async def test_early_release_cancels_pending_when_both_gates_concur(monkeypatch):
    from models.types import ReviewReport, ReviewDirective
    async def fake_review(*a, **k):
        return ReviewReport(directive=ReviewDirective(
            kind="qualified_release", release_specialist="spend_payments"))
    async def fake_re_review(*a, **k):
        return True  # orchestrator concurs
    monkeypatch.setattr(server, "_run_review", fake_review)
    monkeypatch.setattr(server, "_orchestrator_re_review", fake_re_review)

    ran = {"straggler_finished": False}
    async def straggler():
        try:
            await asyncio.sleep(1.0)
            ran["straggler_finished"] = True
        except asyncio.CancelledError:
            raise
    pending = [asyncio.create_task(straggler())]

    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments", "modeling"}
    released = await server._early_release_check(
        sess=None, ctx=ctx, question="q",
        returned_output={"spend_payments": "full answer"}, pending_tasks=pending)

    assert released is True
    assert pending[0].cancelled() or pending[0].done()
    await asyncio.sleep(1.2)
    assert ran["straggler_finished"] is False  # cancelled, not orphaned


@pytest.mark.asyncio
async def test_early_release_declined_when_orchestrator_disagrees(monkeypatch):
    from models.types import ReviewReport, ReviewDirective
    async def fake_review(*a, **k):
        return ReviewReport(directive=ReviewDirective(
            kind="qualified_release", release_specialist="spend_payments"))
    async def fake_re_review(*a, **k):
        return False  # orchestrator does NOT concur
    monkeypatch.setattr(server, "_run_review", fake_review)
    monkeypatch.setattr(server, "_orchestrator_re_review", fake_re_review)
    pending = [asyncio.create_task(asyncio.sleep(0.05))]
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments", "modeling"}
    released = await server._early_release_check(
        sess=None, ctx=ctx, question="q",
        returned_output={"spend_payments": "partial"}, pending_tasks=pending)
    assert released is False
    assert not pending[0].cancelled()
    await pending[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py -q`
Expected: FAIL (`_early_release_check` / `_orchestrator_re_review` not defined).

- [ ] **Step 3: Implement early-release.** In `server.py`:
  - Add `_orchestrator_re_review(sess, ctx, question, output) -> bool` — a short orchestrator-side confirmation (reuse the orchestrator model with a focused "is this single output sufficient to ship? yes/no" prompt), defaulting to `False` on error.
  - Add `_early_release_check(...)` per the Interfaces block: call `_run_review`; if `directive.kind == "qualified_release"`, call `_orchestrator_re_review`; if it returns True, cancel every task in `pending_tasks` (`task.cancel()`), await them with `return_exceptions=True`, and return True; else return False.
  - Wire `_early_release_check` into the dispatch/gather phase's per-specialist `on_tool_end` hook (Task 5): when a specialist returns while others pend on a multi-specialist turn, invoke it once; on release, short-circuit to synthesis from `release_specialist`'s output.
  - Enforce specialist ≤ 2× per turn via the existing per-AppContext specialist call accounting.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_server/test_plan_review_dispatch.py
git commit -m "feat(server): early qualified-release with double-gate + straggler cancel"
```

---

## Task 8: Integration — the trace scenario

**Files:**
- Test: `tests/test_server/test_plan_review_dispatch.py`

**Interfaces:**
- Consumes: everything above. Uses fake specialists (spend→May-2025, modeling→2024) and a fake reviewer that emits `needs_redispatch(specialist="modeling", anchor="2025-05")`, then a re-dispatched modeling that returns 2025-anchored drivers.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_causal_question_redispatches_modeling_anchored(monkeypatch):
    # Fake dispatch: spend returns May-2025 spike; modeling returns 2024 drivers.
    # Fake reviewer: needs_redispatch(modeling, "2025-05").
    # Assert: modeling is re-invoked with "2025-05" in its sub-question, exactly
    # once (dispatch cap = 2), and the final answer references 2025-05 drivers.
    # (Wire via the same monkeypatch seams used in Tasks 6-7:
    #  server._run_review, the dispatch phase, and the re-invocation path.)
    ...
```

Fill `...` with a concrete harness mirroring Task 6/7 monkeypatch seams: assert
(a) `_dispatch_count` ends at 2, (b) the modeling re-invocation sub-question
contains `"2025-05"`, (c) the synthesized answer text contains the 2025-anchored
drivers and not a bare 2024-only narrative.

- [ ] **Step 2: Run test to verify it fails** — Expected: FAIL (assertions unmet before wiring is complete).
- [ ] **Step 3: Fix any wiring gaps** surfaced by the integration test in `server.py` (no new features — only connect Tasks 6–7 seams).
- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/test_server/test_plan_review_dispatch.py tests/test_models/test_review_directive.py tests/test_agent_factories/test_general_specialist_review.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add tests/test_server/test_plan_review_dispatch.py server.py
git commit -m "test(server): integration for causal re-dispatch anchoring"
```

---

## Self-Review

**Spec coverage:**
- §3 roles (orchestrator dispatches, reviewer advises) → Tasks 2, 6, 7.
- §4 flow (plan/dispatch/review/act/synthesize) → Tasks 4, 6.
- §5.1 plan prompts → Tasks 3, 4.
- §5.2 phased-run enforcement → Task 5 (spike) + Task 6.
- §5.3 `ReviewDirective` → Task 1.
- §5.4 orchestrator acts (re-dispatch, qualified-release re-review) → Tasks 6, 7.
- §5.5 straggler cancellation → Task 7.
- §6 caps (≤2 dispatch, ≤2×/specialist, multi-specialist gate) → Tasks 6, 7.
- §8 error handling (reviewer graceful) → Task 6 `_run_review` returns None.
- §9 testing → Tasks 1–8 tests.

**Placeholder scan:** Task 8 Step 1 intentionally leaves the harness body to be filled against the Task 6/7 seams (documented, not a silent TODO). Task 5 is a spike with a concrete deliverable (decision note). All code-bearing steps in Tasks 1–4, 6, 7 contain real code.

**Type consistency:** `ReviewDirective(kind, specialist, anchor, why, release_specialist)` used identically in Tasks 1, 2, 6, 7. Helper names `_is_multi_specialist_turn` / `_run_review` / `_dispatch_count` / `_early_release_check` / `_orchestrator_re_review` are consistent across Tasks 6–8.

**Risk note:** Task 5 (spike) gates the exact server code in Tasks 6–7. If the spike shows the two-agent split (dispatcher/synthesizer) is cleaner than stop-on-tool, Tasks 6–7 bodies adopt that; the Interfaces blocks stay valid either way.
