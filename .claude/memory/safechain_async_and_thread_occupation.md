# SafeChain model calling: `await amodel(...)` build + sync `chain.invoke` in a worker pool

**The bug class (intermittent, prod-only):** "stuck at team construction / specialists can't be assigned" and "input not captured" trace to **occupied threads that never release**. SafeChain's `chain.invoke()` is blocking; a hung call **cannot be killed** (Python can't interrupt a thread blocked in C/IO), so it leaks the worker thread + its firewall-semaphore slot. When the shared default `to_thread` pool (~12) exhausts, the orchestrator's first call (orch cap 2) can't get a thread → no `team_plan`/specialists. A NEW question then queues behind the hung turn on `sess.turn_lock` and hits `turn_queue_wait_timeout` → "input not captured". Rewind frees the **lock** (not the orphaned thread), so it "sometimes" fixes it.

**History:** `0edf891` added thread-local models + a 64-worker pool + per-call timeout; `f15d6b9` (catch-all "fix: safe-chain") **reverted all three** → regression. Don't naively restore them: per-thread on-demand models caused a re-auth/connection storm (also "thread occupying").

## Correct model-calling API (PR #12 — this is the CURRENT approach)

The prod model factory is **`amodel` (ASYNC), imported `from safechain.core.model import amodel`**. It must be **awaited** — it performs token acquisition. `_invoke` now:

1. **Build once via `await amodel(model_id)`**, cache on `self._llm`, **rebuild on 401** (`_aensure_llm` / `_arefresh_llm`, both async).
2. Build the chain `ValidChatPromptTemplate.from_messages([("human","{__input__}")]) | model | StrOutputParser()` and run **`chain.invoke(...)` SYNCHRONOUSLY** in a **dedicated `ThreadPoolExecutor`** (`_SAFECHAIN_EXECUTOR`, env `SAFECHAIN_THREAD_POOL=32`, sized above the firewall concurrency caps so a hung call can't starve the shared default executor — the original "stuck" mechanism), bounded by `asyncio.wait_for(timeout=_SAFECHAIN_CALL_TIMEOUT_S)` (180s, env). On timeout the asyncio side frees the semaphore + turn lock promptly; the worker thread may linger until safechain returns.

**Reference shape (from the private env), not invented:**
```python
chain = prompt | await amodel('gpt-5.2') | StrOutputParser()
res = chain.invoke({...})   # sync invoke; the async work was the build
```

## Superseded: do NOT use `await llm.ainvoke()`

An earlier attempt (PR #3) used the SYNC `model(...)` factory + `await llm.ainvoke(prompt_value)`. **Two problems:** (a) it later called the async `amodel(...)` WITHOUT `await`, so `self._llm` became an un-awaited coroutine — broken (never ran; safechain isn't in dev); (b) `chain.ainvoke` is unreliable anyway — LangChain's generic Runnable `ainvoke` default is `await run_in_executor(self.invoke)`, so LCEL components that don't override it (e.g. the prompt template) hit the threadpool fallback regardless. The correct pattern is async **build** (`amodel`) + sync **invoke** in our own bounded pool. Don't reintroduce `ainvoke`.

## Knobs (all env-tunable; raise locally where the env is slower)

`TURN_WALL_CLOCK_S` (360) · `SPECIALIST_TIMEOUT_S` (240) · `SAFECHAIN_CALL_TIMEOUT_S` (180) · `DISTILLER_TIMEOUT_S` (60) · `REPORT_AGENT_TIMEOUT_S` (45) · `SCREEN_TIMEOUT_S` (30) · `QUEUED_TURN_MAX_WAIT_S` (90) · `SAFECHAIN_THREAD_POOL` (32). Keep ordered: `SAFECHAIN_CALL_TIMEOUT_S` ≤ `SPECIALIST_TIMEOUT_S` ≤ `TURN_WALL_CLOCK_S`; set `QUEUED_TURN_MAX_WAIT_S` ≥ `TURN_WALL_CLOCK_S` so a second question can wait out a slow turn instead of dropping ("input not captured" residual).

**Parity:** the OpenAI path (`firewall_client.py`) is native-async (`await self._base.create(...)`); no amodel/thread machinery there — no mirror needed. See [[feedback_openai_safechain_parity]].

**Validation limit:** safechain isn't installed in dev, so tests mock `amodel` + the LCEL chain (`tests/test_llm/test_safechain_client.py`: build-via-amodel + cache, per-call timeout, 401 rebuild-retry, concurrent overlap). Final behavior must be confirmed in the private env. Related: [[safechain_dual_environment]], [[feedback_safechain_ask_prod_questions]].
