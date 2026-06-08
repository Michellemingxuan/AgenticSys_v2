# SafeChain: use async ainvoke, never sync invoke in a thread

**The bug class (intermittent, prod-only):** "stuck at team construction / specialists can't be assigned" and "input not captured" both trace to **occupied threads that never release**. SafeChain's `chain.invoke()` is blocking; the old code ran it via `asyncio.to_thread` (default executor ~12 workers). A hung call **cannot be killed** (Python can't interrupt a thread blocked in a C/IO call), so it leaked the worker thread + its firewall-semaphore slot forever. Concurrent specialists (`FIREWALL_SPECIALIST_CONCURRENCY`, prod 12) all invoked the **shared** model, which also deadlocks. Once threads exhaust, the orchestrator's first call (orch cap 2) can't get a thread → no `team_plan`/specialists. A NEW question then queues behind the hung turn on `sess.turn_lock` and hits `turn_queue_wait_timeout` (90s) → "input not captured". Rewind sets `cancel_in_flight` → frees the **lock** (but not the orphaned thread), so it "sometimes" fixes it.

**History:** `0edf891` added thread-local models + a 64-worker pool + per-call timeout to fix this; `f15d6b9` (a catch-all "fix: safe-chain") **reverted all three**, reintroducing the regression. Don't naively restore them — per-thread on-demand models caused a re-auth/connection storm (also "thread occupying").

**Root fix (`fix/safechain-async-native`):** SafeChain's chat model is **real async** — it implements `_agenerate`/`_astream` and its retry wrapper `await`s `ainvoke`. So `llm.safechain_client._SafeChainChatCompletions._invoke` now:
1. Formats the prompt **synchronously** (`ValidChatPromptTemplate.from_messages(...).invoke(...)` — instant in-memory work; avoids LangChain's `run_in_executor` fallback for the trivial template step).
2. `await asyncio.wait_for(llm.ainvoke(prompt_value), timeout=_SAFECHAIN_CALL_TIMEOUT_S)` — **real async, no worker thread held during the network wait**, and the timeout genuinely cancels a hung call (cancelling a coroutine, not orphaning a thread). Default 180s, env `SAFECHAIN_CALL_TIMEOUT_S`.

**Key gotcha (per prod inspection):** LangChain's generic Runnable `ainvoke` default is `await run_in_executor(config, self.invoke, ...)`. SafeChain's *model* overrides it (real async), but other LCEL components that DON'T override `ainvoke` (e.g. some prompt templates) still hit the threadpool fallback when you call `chain.ainvoke(...)`. That's why we format the prompt sync and only `await` the model's `ainvoke` directly — keep it that way.

**Parity:** the OpenAI path (`firewall_client.py`) is already native-async (`await self._base.create(...)`, no `to_thread`), so no mirror change was needed — this brings safechain to parity. See [[feedback_openai_safechain_parity]].

**Validation limit:** safechain isn't installed in dev, so tests mock an async model (`tests/test_llm/test_safechain_client.py`: async-path, per-call timeout, 401-refresh, concurrent-overlap). Final behavior must be confirmed in the private env. Related: [[safechain_dual_environment]].

**Possible follow-up tuning:** `_QUEUED_TURN_MAX_WAIT_S` (90s) < `SAFECHAIN_CALL_TIMEOUT_S` (180s) — a queued question can still time out behind a slow (but now bounded) call. Align if queue-timeouts persist after this fix.
