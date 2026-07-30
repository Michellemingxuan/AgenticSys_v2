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
- **Cancelling an in-flight `ainvoke` returns in ~0.00s AND aborts the request** — verified genuinely in-flight (`done_before_cancel=False` against a 30s baseline call).
- **The old sync `chain.invoke`-in-a-threadpool could NOT be interrupted:** a cancelled call's worker thread ran its FULL 20–120s call to completion (probe: `orphan_finished_after≈17.7s`), holding a pool slot and burning Azure quota. Repeated rewrites/rewinds orphaned enough of these to exhaust the pool → the documented "stuck at team construction / input not captured." **This orphan pileup — not a cancellation deadlock — was the root cause of "stuck after interrupting + re-asking."** (At the asyncio level, cancel returns fast either way; the difference is whether the underlying work stops.)

So `ainvoke` is the fix: cancel/timeout aborts the work, no orphans accumulate.

Why the old caveats no longer apply: PR #3 broke because it called `amodel(...)` WITHOUT `await` (un-awaited coroutine) and mixed the sync `model(...)` factory — a *build* bug, unrelated to the invoke path. The generic-Runnable "`ainvoke` → `run_in_executor(self.invoke)`" fallback only bites when the model does NOT override `_agenerate`; **this build does.** The brief `asyncio_*` redaction threads `ainvoke` still spawns are fast and don't pile up.

**Cross-loop caveat (validated in prod):** the server builds a new event loop per turn but caches `self._llm` across them. This build's `ainvoke` survives that reuse (confirmed). If it ever regresses to `Event loop is closed` / "attached to a different loop" (native async clients can be loop-bound), rebuild the model per loop or move to a single persistent loop.

**Separate, still-OPEN issue:** concurrent safechain calls are intermittently ~4× SLOWER (probe: 5 concurrent 122s vs 5 sequential 29s on one run; fast on another) — Azure TPM/RPM throttling + 429 backoff, **NOT fixed by `ainvoke`.** May require capping LLM concurrency or raising quota. See [[feedback_performance_targets]].

## Knobs (all env-tunable; raise locally where the env is slower)

`TURN_WALL_CLOCK_S` (360) · `SPECIALIST_TIMEOUT_S` (240) · `SAFECHAIN_CALL_TIMEOUT_S` (180) · `DISTILLER_TIMEOUT_S` (60) · `REPORT_AGENT_TIMEOUT_S` (45) · `SCREEN_TIMEOUT_S` (30) · `QUEUED_TURN_MAX_WAIT_S` (90) · `SAFECHAIN_THREAD_POOL` (32). Keep ordered: `SAFECHAIN_CALL_TIMEOUT_S` ≤ `SPECIALIST_TIMEOUT_S` ≤ `TURN_WALL_CLOCK_S`; set `QUEUED_TURN_MAX_WAIT_S` ≥ `TURN_WALL_CLOCK_S` so a second question can wait out a slow turn instead of dropping ("input not captured" residual).

**Parity:** the OpenAI path (`firewall_client.py`) is native-async (`await self._base.create(...)`); no amodel/thread machinery there — no mirror needed. See [[feedback_openai_safechain_parity]].

**Validation limit:** safechain isn't installed in dev, so tests mock `amodel` + the LCEL chain (`tests/test_llm/test_safechain_client.py`: build-via-amodel + cache, per-call timeout, 401 rebuild-retry, concurrent overlap). Final behavior must be confirmed in the private env. Related: [[safechain_dual_environment]], [[feedback_safechain_ask_prod_questions]].
