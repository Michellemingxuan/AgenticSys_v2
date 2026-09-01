---
name: safechain-async-and-thread-occupation
description: "Stuck at team construction" / "input not captured" = orphaned safechain worker threads from cancelled turns exhausting the pool; fixed 2026-07-01 by native async `await chain.ainvoke(...)`. But cancellable is not the same as cancels promptly — any blocking call on the event loop makes calls slow AND unkillable at once.
metadata:
  type: project
---

# SafeChain model calling: `await amodel(...)` build + native async `chain.ainvoke`

**RESOLVED (2026-07-01, prod-validated):** the LLM call is now `await chain.ainvoke(...)`, not sync `chain.invoke` in a thread pool. On this build `ainvoke` is genuinely native-async and cancellable, so a rewind/timeout ABORTS the in-flight call instead of orphaning a worker thread — which is what actually caused "stuck after interrupting + re-asking". Details in the "`ainvoke` IS correct" section below; the historical framing in the next paragraph is kept for context.

**The bug class (intermittent, prod-only):** "stuck at team construction / specialists can't be assigned" and "input not captured" trace to **occupied threads that never release**. SafeChain's `chain.invoke()` is blocking; a hung call **cannot be killed** (Python can't interrupt a thread blocked in C/IO), so it leaks the worker thread + its firewall-semaphore slot. When the shared default `to_thread` pool (~12) exhausts, the orchestrator's first call (orch cap 2) can't get a thread → no `team_plan`/specialists. A NEW question then queues behind the hung turn on `sess.turn_lock` and hits `turn_queue_wait_timeout` → "input not captured". Rewind frees the **lock** (not the orphaned thread), so it "sometimes" fixes it.

**History:** `0edf891` added thread-local models + a 64-worker pool + per-call timeout; `f15d6b9` (catch-all "fix: safe-chain") **reverted all three** → regression. Don't naively restore them: per-thread on-demand models caused a re-auth/connection storm (also "thread occupying").

## Correct model-calling API (this is the CURRENT approach)

The prod model factory is **`amodel` (ASYNC), imported `from safechain.core.model import amodel`**. It must be **awaited** — it performs token acquisition. `_invoke`:

1. **Build once via `await amodel(model_id)`**, cache on `self._llm`, **rebuild on 401** (`_aensure_llm` / `_arefresh_llm`, both async).
2. Build the chain and run **`await chain.ainvoke(...)`**, bounded by `asyncio.wait_for(timeout=_SAFECHAIN_CALL_TIMEOUT_S)` (180s, env). (Chain shape as of the 2026-07-29 rewrite: `ValidChatPromptTemplate.from_messages([MessagesPlaceholder("messages")]) | model.bind(**openai_kwargs)` — see [[safechain_dual_environment]]. The async/cancellation behavior below is unaffected by that change; streaming uses `astream` and bounds the GAP BETWEEN chunks.) Because `ainvoke` is native async here, `wait_for` / task cancellation ABORT the in-flight request and free the semaphore + turn lock promptly — **no lingering worker thread**.

**Reference shape (from the private env), not invented:**
```python
chain = prompt | await amodel('gpt-5.2') | StrOutputParser()
res = await chain.ainvoke({...})   # native async invoke; build was also async
```

`_SAFECHAIN_EXECUTOR` (env `SAFECHAIN_THREAD_POOL=32`) is **retained but no longer runs the LLM call** — keep it only for optionally pinning the loop default executor (`loop.set_default_executor(_SAFECHAIN_EXECUTOR)`) so safechain's brief internal redaction offload lands on a sized pool.

## `ainvoke` IS correct on this build (verified 2026-07-01, prod)

Earlier this note said "do NOT use `ainvoke`." **Overturned** by empirical probing in the private env. Findings on the current model (`SafeAzureChatOpenAI`, MRO `… InputRedactor → OpenAIMiddleware → AzureChatOpenAI → BaseChatOpenAI`):

- **`_agenerate` IS overridden by the model** (real native-async path), not the generic Runnable fallback.
- **Cancelling an in-flight `ainvoke` returns in ~0.00s AND aborts the request** — verified genuinely in-flight (`done_before_cancel=False` against a 30s baseline call). **But only when the event loop is free to deliver the cancellation** — see "Cancellation is only as prompt as the loop" below, which is how this note misled a later investigation.
- **The old sync `chain.invoke`-in-a-threadpool could NOT be interrupted:** a cancelled call's worker thread ran its FULL 20–120s call to completion (probe: `orphan_finished_after≈17.7s`), holding a pool slot and burning Azure quota. Repeated rewrites/rewinds orphaned enough of these to exhaust the pool → the documented "stuck at team construction / input not captured." **This orphan pileup — not a cancellation deadlock — was the root cause of "stuck after interrupting + re-asking."** (At the asyncio level, cancel returns fast either way; the difference is whether the underlying work stops.)

So `ainvoke` is the fix: cancel/timeout aborts the work, no orphans accumulate.

Why the old caveats no longer apply: PR #3 broke because it called `amodel(...)` WITHOUT `await` (un-awaited coroutine) and mixed the sync `model(...)` factory — a *build* bug, unrelated to the invoke path. The generic-Runnable "`ainvoke` → `run_in_executor(self.invoke)`" fallback only bites when the model does NOT override `_agenerate`; **this build does.** The brief `asyncio_*` redaction threads `ainvoke` still spawns are fast and don't pile up.

**Cross-loop caveat (validated in prod):** the server builds a new event loop per turn but caches `self._llm` across them. This build's `ainvoke` survives that reuse (confirmed). If it ever regresses to `Event loop is closed` / "attached to a different loop" (native async clients can be loop-bound), rebuild the model per loop or move to a single persistent loop.

## Cancellation is only as prompt as the LOOP (2026-08-25, prod)

The section above is right about safechain and **wrong as a diagnostic**, which
cost several rounds of debugging. `ainvoke` being cancellable says nothing
about how fast a cancel LANDS, because `asyncio.wait_for` cancels the task and
then awaits it — and none of that runs if some other coroutine on the same
loop is inside a **blocking** call.

**The incident.** On the private SERVER (not the private env — that was fine),
turns took ~30s instead of ~2s and cancelling one took ~20s. The 10s screen
fence fired, `wait_for` did not return for another ~22s, so the retry inherited
a spent budget and `screen_retry_stalled` + `screen_timeout` followed. It read
exactly like "safechain is not cancellable on this build". It was not.

**Root cause: `tiktoken`.** `_estimate_tokens` called
`tiktoken.encoding_for_model(...)`, which fetches its BPE file on first use.
`tiktoken/load.py` does a bare `requests.get(blobpath)` with **no timeout**, so
a host with no egress to `openaipublic.blob.core.windows.net` waits out the OS
TCP connect timeout with urllib3 retries on top. It ran **twice per LLM round**
(prompt + completion), from inside coroutines, and the old
`except Exception: return len(text) // 4` caught the failure without
REMEMBERING it — so every call paid it again. The loop was pinned, not
awaiting. Fixed by caching the encoder AND its unavailability in
`tools/node_trace/pricing.py` (one implementation; there had been two, which is
what hid it). `TIKTOKEN_LOAD_TIMEOUT_S=0` opts an air-gapped host out entirely.

**The rule to carry forward:** before blaming the transport for a slow or
unkillable call, ask *what else on this loop blocks*. Sync network I/O, sync
SQLite, and anything doing a hidden first-use download all masquerade as
transport problems, and they produce SLOW + UNCANCELLABLE together — which is
the signature that sent this investigation at safechain.

**Diagnostic ladder that actually worked** (in order; each cut the space in half):

1. `tools/safechain_probe.py` — now prints a LATENCY block. Server median
   **990ms** vs private env **1223ms**: the server was FASTER, exonerating the
   transport and turning the search inward. Capability tables alone said
   "both work" and hid a 30x difference.
2. `tools/llm_latency_bisect.py` — times raw safechain / + our prep / + the
   gate / the full client call, in one process. L4 (literally the prewarm
   call) was **1.28s on the server**, versus 43s during boot. So: not the
   transport, not our wrapper, not the gate.
3. `NODE_TRACE_DISABLE=1` made it fast — which pointed at the tracing BRANCH,
   not the trace DB. Moving `NODE_TRACE_DB` to local disk changed nothing.
   The give-away was that prewarm was fine: there is no active node at boot,
   so `create()` early-returns past that branch entirely.

**Wrong turns, recorded so they are not repeated:** the trace DB on an NFS home
(`/adshome`) with WAL rejected — plausible, measurably false; BLAS/OpenMP
thread starvation on a many-core node — plausible, untested, unnecessary;
`response_format` on this deployment — exonerated by prewarm, which sends none
and was still slow.

**Separate, still-OPEN issue:** concurrent safechain calls are intermittently ~4× SLOWER (probe: 5 concurrent 122s vs 5 sequential 29s on one run; fast on another) — Azure TPM/RPM throttling + 429 backoff, **NOT fixed by `ainvoke`.** May require capping LLM concurrency or raising quota. See [[feedback_performance_targets]].

## Knobs (all env-tunable; raise locally where the env is slower)

`TURN_WALL_CLOCK_S` (360) · `SPECIALIST_TIMEOUT_S` (240) · `SAFECHAIN_CALL_TIMEOUT_S` (180) · `DISTILLER_TIMEOUT_S` (60) · `REPORT_AGENT_TIMEOUT_S` (45) · `SCREEN_TIMEOUT_S` (30) · `QUEUED_TURN_MAX_WAIT_S` (90) · `SAFECHAIN_THREAD_POOL` (32) · `TIKTOKEN_LOAD_TIMEOUT_S` (5; set **0** on an air-gapped host). Keep ordered: `SAFECHAIN_CALL_TIMEOUT_S` ≤ `SPECIALIST_TIMEOUT_S` ≤ `TURN_WALL_CLOCK_S`; set `QUEUED_TURN_MAX_WAIT_S` ≥ `TURN_WALL_CLOCK_S` so a second question can wait out a slow turn instead of dropping ("input not captured" residual).

**Parity:** the OpenAI path (`firewall_client.py`) is native-async (`await self._base.create(...)`); no amodel/thread machinery there — no mirror needed. See [[feedback_openai_safechain_parity]].

**Validation limit:** safechain isn't installed in dev, so tests mock `amodel` + the LCEL chain (`tests/test_llm/test_safechain_client.py`: build-via-amodel + cache, per-call timeout, 401 rebuild-retry, concurrent overlap). Final behavior must be confirmed in the private env. Related: [[safechain_dual_environment]], [[feedback_safechain_ask_prod_questions]].
