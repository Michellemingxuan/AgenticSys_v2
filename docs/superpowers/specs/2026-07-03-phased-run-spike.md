# Phased-Run Stop/Resume Mechanic — SPIKE Decision Note

Date: 2026-07-04
Status: Resolved (spike complete)
Scope: resolves design spec §5.2 / §10.1 open item — the exact `Runner`
stop/resume mechanics for the phase-1 (dispatch+gather) → phase-3 (synthesize)
handoff. Downstream Tasks 6–8 build on this.

SDK under test: `openai-agents==0.3.3`
(`~/.pyenv/versions/3.11.13/envs/autoAI/lib/python3.11/site-packages/agents`).
All findings below are verified against SDK **source** and a **runnable
stub-model prototype** (no live OpenAI calls); the prototype lives at
`scratchpad/phased_run_spike.py` (not committed).

---

## 1. Chosen mechanism (one line)

**Two `Runner` runs on a single orchestrator agent, with state carried across
the phase boundary by `result.to_input_list()` and the reviewer directive
injected as an appended `{"role":"user"}` message before the second run.**

This is design-spec approach **(c)**. It won over (a) and (b) for the reasons
in §5.

---

## 2. Ending the dispatch phase (phase 1)

The orchestrator dispatches specialists (parallel where possible — one turn with
multiple `@function_tool` calls), the SDK runs them via `asyncio.gather`, feeds
the results back, and the orchestrator emits a short **dispatch-summary text**
as its `final_output`. That text ending the run IS the deterministic phase-1
boundary — no special SDK stop mechanism required.

Server code (phase 1) — keep `run_streamed` so SSE keeps flowing:

```python
streamed = Runner.run_streamed(orchestrator, run_input, context=ctx, hooks=trace_hooks)
async for ev in streamed.stream_events():
    ...  # existing SSE emit
# phase-1 boundary reached when the stream drains:
p1_final = streamed.final_output          # dispatch-summary text
p1_items = streamed.new_items             # includes every ToolCallOutputItem
p1_input = streamed.to_input_list()       # full transcript for resume seeding
```

`RunResultStreaming` extends `RunResultBase` (`result.py:125`), so
`to_input_list()` (`result.py:95`) and `new_items` (`result.py:46`) are all
available on the streamed result once the stream has drained — no extra plumbing.

**Verified (stub):** phase-1 run returns its dispatch-summary as `final_output`;
`new_items` carries both specialist `ToolCallOutputItem`s.

---

## 3. Recovering specialist outputs in server code (phase boundary)

Two independent, both-verified paths:

**(a) After the run — from `new_items`.** Filter `ToolCallOutputItem`; the
tool's returned string is `item.output`, and the originating call id is
`item.raw_item["call_id"]` (raw_item is the `function_call_output` dict):

```python
specialist_outputs = {}
for item in streamed.new_items:
    if item.__class__.__name__ == "ToolCallOutputItem":
        specialist_outputs[item.raw_item["call_id"]] = item.output
```

**(b) Live, as each specialist returns — from the hook.** `RunHooks.on_tool_end`
(`lifecycle.py`) fires **once per tool, as that tool completes**, inside its own
`run_single_tool` task **before** the outer `asyncio.gather` collects
(`_run_impl.py:787`). Signature:

```python
async def on_tool_end(self, context, agent, tool, result: str) -> None: ...
```

`tool.name` identifies the specialist; `result` is its output string. This is
the hook that drives **early qualified-release** (§5.2 of the design): the server
sees a specialist finish while others still pend.

**Verified (stub):** `on_tool_end` fired per specialist
(`['spend_payments','modeling', ...]`), each with its output string, and the
straggler (`asyncio.sleep`) fired its own `on_tool_end` when it independently
completed.

---

## 4. Injecting the reviewer directive + resuming (phase 3)

Seed the resumed run with the **entire** phase-1 transcript plus one appended
user message carrying the `ReviewDirective`:

```python
resume_input = p1_input                    # = streamed.to_input_list()
resume_input.append({
    "role": "user",
    "content": f"[REVIEW DIRECTIVE] {directive.kind}: {directive.specialist} "
               f"anchor={directive.anchor}. why: {directive.why} ...",
})
phase3 = Runner.run_streamed(orchestrator, resume_input, context=ctx, hooks=trace_hooks)
async for ev in phase3.stream_events(): ...   # re-dispatch (if directed) + synthesize
final_answer = phase3.final_output            # FinalAnswer
```

The resumed orchestrator sees the full dispatch history + the directive and
either re-dispatches the named specialist anchored to `anchor`, then synthesizes,
or synthesizes directly (`coherent`).

**Verified (stub):** `to_input_list()` produced a 7-item transcript; appending
the directive and running a second time re-dispatched `modeling` anchored to
`2025-05` and produced the anchored final answer. State crossed the boundary
cleanly with no manual message reconstruction.

**Note on the agent object:** the prototype reuses one `Agent` across both runs.
`Runner.run*` has no `model=`/`instructions=` override kwarg — per-phase config
belongs on the agent. If phase 3 needs different instructions (e.g. a
synthesize-focused prompt), use `orchestrator.clone(instructions=...)`
(`agent.py:367`) rather than mutating the shared instance.

---

## 5. Why (c), not (a) sentinel / (b) two agents

- **(a) `tool_use_behavior` / `StopAtTools` / sentinel — REJECTED.** Confirmed in
  source (`_run_impl.py:1206-1213`) and stub: `StopAtTools` sets `final_output`
  to the **first matching tool's output only**; `stop_on_first_tool` likewise.
  A *parallel* dispatch set is therefore **not** captured as the run's final
  output — the stub showed `final_output` = just `spend_payments`, with `modeling`
  only reachable via `new_items`. So the sentinel buys nothing over reading
  `new_items`, and a sentinel "dispatch_done" tool forces an extra orchestrator
  turn to call it. A `ToolsToFinalOutputFunction` (callable `tool_use_behavior`,
  `_run_impl.py:1214`) *could* gather, but it fights the resume-seeding pattern
  and adds a code path per backend. Not worth it.
- **(b) Two separate agents (dispatcher + synthesizer) — WORKABLE, NOT CHOSEN.**
  Requires manually threading the gathered outputs into a fresh synthesizer input
  and duplicating orchestrator config. (c) achieves the same with one agent and
  the SDK's own `to_input_list()` carrying state natively — less surface area.
- **(c) Two runs + `to_input_list()` — CHOSEN.** Cleanest: one agent, native
  state transfer, directive is just an appended message, works identically under
  `run` and `run_streamed`.

---

## 6. Cancellation / early-release handle

Parallel specialists dispatched in one turn all run under a single
`asyncio.gather` **inside the run's own task** (`_run_impl.py:818`). There is no
public API to cancel *one* in-flight tool while letting the same run continue —
and that is fine, because early-release means abandoning the stragglers and
ending the dispatch phase anyway. The concrete handles:

- **`run_streamed` path:** `RunResultStreaming.cancel()` (`result.py:171`) —
  cancels all background run tasks (`_run_impl_task` etc.), drains the queue,
  marks complete.
- **`Runner.run` path:** wrap it — `task = asyncio.create_task(Runner.run(...))`
  then `task.cancel()`.

The early-released winner's output is **already captured** by `on_tool_end`
(§3b) *before* you cancel, so you lose nothing by killing the run. Sequence for
qualified-release:

1. `on_tool_end` fires for the fast specialist → server stashes `result`.
2. Server fires the early reviewer; on double-gate concurrence (§5.4)…
3. …server calls `streamed.cancel()` (or `task.cancel()`) → stragglers abort.
4. Server proceeds to phase 3 seeded with the stashed winner output.

**Verified (stub):** wrapping `Runner.run` in a task and calling `.cancel()`
during a 10s specialist `sleep` raised `CancelledError` and unwound the run
(`task_cancelled: True`).

**Reconciles with the design:** early-release is a hook-observed, between-run
cancellation of the *whole dispatch run*, not a surgical single-tool cancel. The
phased structure (§5.2) makes that the natural shape.

---

## 7. Minimal verified prototype (excerpt)

Full runnable file: `scratchpad/phased_run_spike.py`. Core loop:

```python
# PHASE 1 — dispatch+gather (stub model scripts 2 parallel tool calls, then text)
r1 = await Runner.run(orch, question, hooks=hooks)
outs = {it.raw_item["call_id"]: it.output
        for it in r1.new_items if it.__class__.__name__ == "ToolCallOutputItem"}

# PHASE BOUNDARY — recover + inject directive
resume = r1.to_input_list()
resume.append({"role": "user",
               "content": "[REVIEW DIRECTIVE] needs_redispatch: modeling "
                          "anchor=2025-05. why: drivers (2024) not anchored to spike."})

# PHASE 3 — resume: re-dispatch anchored + synthesize
r2 = await Runner.run(orch, resume, hooks=hooks)   # -> anchored FINAL answer
```

Observed output:
```
recovered_outputs: ['spend spike ... May 2025', 'TSR score spike 2024-09 ...']
on_tool_end_fired: ['spend_payments', 'modeling', 'modeling']
phase3_final:      FINAL: spend spiked May 2025; drivers anchored to May 2025.
resume_input_len:  7
```

---

## 8. Backend-agnostic vs. needs-prod-safechain validation

### Backend-agnostic (SDK-level control flow — safe to build on now)

These depend only on `Runner`/`RunResult`/`RunHooks` mechanics, which sit
*above* the `Model` implementation. Verified with a stub `Model`:

1. Phase boundary = orchestrator emitting final text ends the run; recover state
   via `result.to_input_list()` + `result.new_items`.
2. `ToolCallOutputItem.output` / `raw_item["call_id"]` recovery of specialist
   outputs.
3. `RunHooks.on_tool_end(ctx, agent, tool, result)` fires once per specialist as
   it completes (design already relies on `trace_hooks` firing in prod — this is
   the same lifecycle).
4. Directive injection as an appended `{"role":"user"}` item; resume via a second
   `Runner.run*` on the same (or `.clone()`d) agent.
5. Cancellation via `RunResultStreaming.cancel()` or `task.cancel()`.

### Needs prod-safechain validation before Tasks 6–8 ship

Cannot be exercised in dev (safechain is prod-only). Flagged risks:

1. **Parallel-tool-in-one-turn assumption.** safechain's parallel-tool-call
   semantics are non-native (duplicate calls collapse into the existing dedup
   cache). Confirm whether the orchestrator actually emits ≥2 specialist tool
   calls in a *single* turn under safechain, or serializes them across turns.
   Either way the mechanic holds — `new_items` accumulates across turns and
   `on_tool_end` still fires per specialist — but the "one `asyncio.gather`"
   mental model for cancellation only applies to a genuine single-turn parallel
   dispatch. **Validate the turn shape in a prod trace.**
2. **Cancellation actually aborts the in-flight LLM call.** `.cancel()` unwinds
   the *SDK* task cleanly (verified). Whether it aborts the underlying safechain
   `_invoke` without leaking a pool thread depends on the `ainvoke`/cancellable
   path per `.claude/memory/safechain_async_and_thread_occupation.md`. **Highest
   prod risk — verify no orphaned thread after an early-release cancel.**
3. **`_FakeAsyncStream` under `run_streamed`.** safechain's `_invoke` returns a
   synthetic single-chunk stream (non-streaming under the stream). Phase
   boundaries do not depend on token-level streaming, but confirm
   `run_streamed().stream_events()` drains normally and `final_output` /
   `new_items` / `to_input_list()` populate on the streamed result under
   safechain (they already must, since prod uses `run_streamed` today).
4. **`on_tool_end` `result` shape under the redaction stack.** Confirm the
   `result` string the hook receives matches the post-redaction specialist output
   the server expects to stash for early-release.

---

## 9. Concrete API decisions for Tasks 6–8

| # | Decision | Exact SDK surface |
|---|----------|-------------------|
| 1 | Phase 1 = one `run_streamed`; boundary = stream drains | `Runner.run_streamed(...)`, `streamed.final_output` |
| 2 | Carry state across boundary | `RunResultBase.to_input_list()` (`result.py:95`) |
| 3 | Recover specialist outputs | `result.new_items` → `ToolCallOutputItem.output` / `.raw_item["call_id"]` |
| 4 | Observe per-specialist completion / early-release | `RunHooks.on_tool_end(ctx, agent, tool, result)` (`lifecycle.py`) |
| 5 | Inject directive + resume (phase 3) | append `{"role":"user","content":...}`; second `Runner.run_streamed`; per-phase config via `Agent.clone()` (`agent.py:367`) |
| 6 | Cancel stragglers / early-release | `RunResultStreaming.cancel()` (`result.py:171`) or `task.cancel()` |

Rejected: `tool_use_behavior=StopAtTools`/`stop_on_first_tool` (captures only the
first tool as `final_output` — `_run_impl.py:1202-1213`).
