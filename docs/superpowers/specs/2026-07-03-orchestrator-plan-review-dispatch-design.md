# Orchestrator Plan–Review Dispatch — Design Spec

Date: 2026-07-03
Status: Draft (for review)
Owner: orchestration / server

## 1. Problem

Multi-specialist turns answering **causally-dependent** questions produce
answers that are correct-per-specialist but **disconnected**, because
specialists are dispatched in parallel and isolated (they cannot see each
other's outputs), and nothing reconciles them before synthesis.

### Observed trace (case 366132845011, turn `4bcb9a660e85`)

Question: *"did the spending spike, what drives it?"* Two specialists ran in
parallel:

| Specialist | Distilled topics | Time period |
|---|---|---|
| `spend_payments` | `monthly_spend_trend`, `merchant_concentration_may_2025`, `industry_concentration_may_2025` | spike = **May 2025** |
| `modeling` | `tsr_score_spike_2024_09`, `oop_interaction_peak_2024_05`, `tsr_spike_drivers_2024_09` | drivers = **2024** |

The "what drives it" half describes model risk events from **2024** — 8–12
months before the actual **May 2025** spike. The `modeling` specialist never
learned when the spike was, so it anchored to its own detected events. The log
shows `general_specialist_skipped` (`n_domain_specialists: 2`) — the review
step that could have caught this did not run. Synthesis (`synthesis.md` Path
A/B) relayed both halves into a coherent-sounding but uninformative answer.

**Root cause:** a causally-dependent question ("what drives X" needs X's
timing) was dispatched as if independent, with no enforced reconciliation.

Note: the `modeling` specialist surfacing CDSS/TSR output scores is **correct**
(those scores gate transaction approval) — not a lane violation. The only
defect is the missing *anchoring* of the driver analysis to the spike window.

## 2. Goals / Non-goals

**Goals**
- The orchestrator acts as an excellent manager ("VP"): dispatches the team by
  judgment, reviews the deliverable, and ships a sharp, coherent answer in the
  fewest rounds.
- Parallel dispatch stays the default (efficiency); extra rounds only when a
  dependency genuinely requires them.
- A dedicated, **server-enforced** review step reliably runs on multi-specialist
  turns and cannot be skipped.
- Support early release: if one specialist already fully answers, ship it and
  cancel the stragglers.

**Non-goals**
- No enumerated catalog of "dependent question" patterns — the orchestrator uses
  general judgment (the manager metaphor), not rules.
- No general DAG planner / structured plan object (over-engineered; fights the
  judgment-not-scaffolding ethos).
- Not fixing Azure throttling (separate operational issue). This design *tolerates*
  slow stragglers by cancelling them, but does not remove the throttling.

## 3. Roles (clean separation)

- **Orchestrator (VP)** — owns ALL dispatch: plans the shape, dispatches the
  **domain specialists**, decides and performs **re-dispatch**, cancels
  stragglers, and synthesizes. The reviewer is not a team member it adds.
- **general_specialist (reviewer)** — **review only.** Inspects domain
  specialists' outputs; *flags* incoherence / judges qualified-release; *suggests*
  an anchor. It does NOT dispatch, does NOT produce domain analysis, does NOT
  substitute for a specialist. It keeps its verification-only tools
  (`aggregate_column`, `get_table_schema`, …) solely to *check* a date/anchor.

## 4. Turn flow

```
1. PLAN (up-front, VP judgment — prompt)
     orchestrator picks a dispatch shape:
       • parallel   — independent sub-questions (DEFAULT)
       • collapse   — one cross-querying specialist self-anchors
                      (e.g. "modeling: find the spend-spike month from
                       spends_data yourself, then analyze drivers around it")
       • sequential — dispatch the anchor specialist first, thread its
                      result into the dependent specialist's sub-question

2. DISPATCH → specialists run (parallel where possible)

3. REVIEW (server-enforced; only when ≥2 specialists DISPATCHED)
     general_specialist, review-only, TWO entry conditions:
       (a) EARLY qualified-release — a returned output appears to fully
           answer the question while others are still pending → judge
           "qualified to release?"
       (b) POST-DISPATCH coherence — all dispatched specialists returned →
           judge "do the pieces cohere? is each driver/explanation anchored
           to the event it explains?"
     Output: a DIRECTIVE (see §6). The reviewer never acts.

4. ORCHESTRATOR ACTS on the directive:
       • qualified-release → orchestrator RE-REVIEWS the single output itself
                             (independent 2nd gate — the kill is irreversible);
                             if it concurs → CANCEL still-in-flight specialists
                             + synthesize from it; else await the others (normal
                             coherence flow)
       • needs-redispatch  → re-invoke the named specialist anchored to W
                             (only if dispatch-count < 2)
       • coherent / capped → synthesize (flag if still imperfect at the cap)

5. SYNTHESIZE → final answer
```

## 5. Component changes

### 5.1 Plan — prompt-only

- `agent_factories/orchestrator_agent.py` (~line 101): remove the "emit every
  specialist in a SINGLE response so they run in parallel" mandate. Replace with
  the VP framing + the three dispatch shapes + the balance rule ("parallel-first;
  add a round only when a dependency needs it; you may collapse a dependency into
  one cross-querying specialist"). `parallel_tool_calls=True` stays (parallel is
  still the default and common case).
- `skills/workflow/team_construction.md`: add a "Dispatch shape" section
  describing parallel / collapse / sequential and when the VP prefers each,
  leveraging that specialists can query any table. **Remove** the row-31
  restriction "`modeling` = ML-derived spend features only … NOT TSR/CDSS."
  CDSS/TSR are the central scores of the credit-and-risk pillar and gate
  transaction/spend approval — the modeling specialist should reference them
  for ANY relevant question, not just spend features. This is a content-scope
  fix (what modeling may surface), independent of the dispatch decision
  (whether the VP adds modeling to the team, which the dispatch-shape guidance
  governs).

### 5.2 Review — server-enforced reviewer

- **Enforcement — DECIDED: server-driven phased run + hook-driven early-release.**
  Chosen with safechain in mind. Safechain's underlying call is non-streaming
  (`_invoke` returns a synthetic `_FakeAsyncStream`) and its parallel-tool-call
  semantics are non-native (duplicate calls → the existing dedup cache), so
  token/stream-level interception is unreliable. But tool-lifecycle hooks already
  work on safechain (`trace_hooks` run in prod) and cancellation is now clean
  (`ainvoke`). Therefore:
  - **Backbone = phased run.** The server drives the turn as explicit phases at
    `Runner` boundaries (it already models `turn_phase_*`): (1) **dispatch+gather**
    — run the orchestrator to dispatch the specialists and gather their outputs;
    (2) **review** — the server invokes `general_specialist` in server code
    (guaranteed, un-skippable); (3) **synthesize** — the server resumes the
    orchestrator with the `ReviewDirective` injected (re-dispatch if directed,
    then synthesize). Controlling the orchestrator at Runner boundaries avoids
    fighting safechain's synthetic stream and non-native parallel-tool rounds.
  - **Early-release = hooks.** During phase 1, the existing SDK tool-lifecycle
    hooks observe each specialist's completion; when one finishes while others
    pend, the server fires the early reviewer, and on the double-gate concurrence
    (§5.4) cancels the pending specialist tasks. Cancellation is clean thanks to
    `ainvoke` (no orphaned threads).
  - **Single-specialist turns keep the current single-run path** (no phasing, no
    reviewer) — zero added latency.
  This phasing is the largest implementation change; the plan sequences it first.
- `agent_factories/general_specialist.py` + `skills/workflow/comparison.md`:
  broaden the reviewer from value-contradiction detection to **coherence /
  alignment** review and **qualified-release** judgment. It must be able to say:
  "specialist X's driver analysis (period P1) is not anchored to the event
  (period P2) it is meant to explain → re-dispatch X anchored to P2," and "this
  single output fully and coherently answers the question → release."

### 5.3 Reviewer output schema

Extend `ReviewReport` / `Resolution` in `models/types.py` with a directive the
orchestrator acts on (generalizing the existing `corrected_specialist` /
`corrected_value` re-invocation path from "adopt this value" to "re-run anchored
to this window"):

```
ReviewDirective:
  kind: "coherent" | "needs_redispatch" | "qualified_release"
  # needs_redispatch:
  specialist:      str | None        # who to re-run
  anchor:          str | None        # the window/event to anchor to (e.g. "2025-05")
  why:             str | None        # one line, for the sub-question + flags
  # qualified_release:
  release_specialist: str | None     # whose output is sufficient to ship
```

The existing `corrected_specialist` / `corrected_value` fields are retained /
mapped for backward compatibility with contradiction resolutions.

### 5.4 Orchestrator acts

- **needs_redispatch:** if dispatch-count < 2, re-invoke `specialist` with
  `anchor` + `why` threaded into the sub-question (reuse the post-review
  re-invocation round). Else synthesize with a flag.
- **qualified_release:** the orchestrator **re-reviews** `release_specialist`'s
  output itself — an independent second gate, because the kill is irreversible.
  Only if it concurs does it cancel the still-in-flight specialist tasks and
  synthesize from that output. If it disagrees, it discards the early release,
  awaits the remaining specialists, and proceeds to the normal coherence review.
- **coherent:** synthesize.

### 5.5 Straggler cancellation

- The orchestrator cancels the in-flight specialist tasks for the turn (reuse the
  turn's cancel path). Because specialists now run their LLM call via cancellable
  `ainvoke` (see `.claude/memory/safechain_async_and_thread_occupation.md`), a
  stuck straggler **aborts** rather than lingering on a pool thread — the two
  changes compound.

## 6. Caps & guardrails (efficiency)

- **≤ 2 dispatch rounds per turn** — initial dispatch + at most ONE re-dispatch.
  No further loops; if still imperfect at the cap, synthesis flags it.
- **A specialist is invoked ≤ 2× per turn** — so a re-dispatched specialist
  carries at most one prior exchange as payload (aligns with the existing
  `_SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES = 2`).
- **Review only on multi-specialist turns** (≥2 specialists dispatched).
  Single-specialist turns skip the reviewer entirely — zero added latency.
- **Parallel is the default shape.** Review is a cheap (~1200-token) gate; the
  expensive extra round (re-dispatch) fires only on genuine incoherence.

## 7. Data flow

```
question ──▶ orchestrator PLAN ──▶ dispatch domain specialists (parallel/…)
                                        │
              specialist outputs ◀──────┘  (some may still be pending)
                    │
   server enforces ▼ REVIEW (general_specialist, review-only)
                    │   early: qualified_release?   post: coherent? / needs_redispatch?
                    ▼
             ReviewDirective ──▶ orchestrator acts
                    │                    ├─ re-review → (concur) cancel stragglers + synthesize  (qualified_release)
                    │                    ├─ re-dispatch anchored (≤1)         (needs_redispatch)
                    │                    └─ synthesize (+flag if capped)      (coherent/capped)
                    ▼
               final answer
```

## 8. Error handling

- **Reviewer fails / times out:** degrade gracefully — synthesize from whatever
  specialist outputs exist (never block the turn on the reviewer). Log it.
- **Re-dispatched specialist fails:** fall back to the pre-re-dispatch outputs +
  a flag; do not loop.
- **Cancellation of stragglers:** best-effort; a cancel that can't land must not
  wedge the turn (the `ainvoke` change makes this clean, but the turn proceeds
  regardless).
- **Cap reached with residual incoherence:** synthesize and flag
  ("driver analysis not fully anchored to the event window") — never exceed 2
  dispatches.

## 9. Testing

- **Reviewer unit:** emits `needs_redispatch` on a misaligned pair (May-spike /
  Apr-drivers) with the correct `anchor`; emits `coherent` on an aligned pair;
  emits `qualified_release` when one output fully answers.
- **Caps unit:** dispatch-count stops at 2; specialist-invocation stops at 2.
- **Cancellation unit:** on `qualified_release`, pending specialist tasks are
  cancelled and do not contribute to synthesis; no orphaned work (leverages the
  `ainvoke` no-orphan guarantee).
- **Enforcement unit:** reviewer runs on ≥2-specialist turns; is skipped on
  single-specialist turns.
- **Integration (the trace scenario):** "what drives the spike" yields a
  spike-anchored driver analysis, or a flag if it can't within the cap.

## 10. Open questions / risks

1. *(Resolved — see §5.2)* **Enforcement = server-driven phased run + hook-driven
   early-release**, chosen for safechain compatibility. Remaining detail for the
   plan: the exact `Runner` stop/resume mechanics for the phase-1 → phase-3
   handoff (tool_use_behavior / re-run with injected input) under both the OpenAI
   and safechain backends.
2. *(Resolved)* **Early-release = two-layer gate.** On an early return (a
   specialist finishing while others pend), the reviewer examines that single
   output for qualified-release; if okay, the orchestrator independently
   re-reviews before killing the stragglers. Both must concur. Bounded: the early
   check runs on an early return (few per turn under the ≤2-dispatch cap), and a
   partial output is rejected quickly by the reviewer.
3. **Latency budget** — confirm the review pass fits inside `TURN_WALL_CLOCK_S`
   with the ≤2-dispatch cap under safechain throttling.
4. *(Resolved)* **team_construction row-31** — the "NOT TSR/CDSS" content
   restriction is removed outright (CDSS/TSR are the credit-risk pillar; modeling
   references them for any relevant question). This is content-scope only; it does
   NOT widen *when* modeling is dispatched — that stays governed by the
   dispatch-shape / team-selection guidance.

## 11. Files touched (anticipated)

- `agent_factories/orchestrator_agent.py` — VP framing, drop forced-parallel.
- `skills/workflow/team_construction.md` — dispatch-shape guidance; row-31 fix.
- `agent_factories/general_specialist.py`, `skills/workflow/comparison.md` —
  reviewer: coherence + qualified-release, review-only.
- `models/types.py` — `ReviewDirective` (+ retain `corrected_specialist`).
- `server.py` — server-enforced review interception, straggler cancellation,
  the ≤2 dispatch / ≤2-per-specialist caps, multi-specialist gating.
- Tests under `tests/` mirroring §9.
```
