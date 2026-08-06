---
name: feedback-openai-safechain-parity
description: Any change to the OpenAI LLM client path must be mirrored in the SafeChain path — they are parallel implementations of the same interface.
metadata:
  type: feedback
---

The system has two LLM transport backends:
- `llm/firewall_client.py` — OpenAI API path (dev/test)
- `llm/safechain_client.py` — SafeChain pipeline path (private/prod)

Both implement the same `chat.completions.create()` interface but with different internals. **Any change to one must be considered for the other.**

**Why:** The private/prod environment uses SafeChain exclusively. If a bug fix, telemetry improvement, or behavioral change is applied only to the OpenAI path, it silently diverges from prod behavior, and bugs discovered in dev may not reproduce in prod (or vice versa).

**How to apply:** When modifying `firewall_client.py`:
1. Check if the equivalent logic exists in `safechain_client.py`
2. If yes → apply the same change. Since the 2026-07-29 rewrite the two paths are
   MUCH closer than they were, so most changes port directly:
   - Messages are role-tagged lists on both. (SafeChain no longer flattens to one string.)
   - Tool calls are native on both. (SafeChain no longer injects schemas as text.)
   - `usage` is real on both — SafeChain maps `AIMessage.usage_metadata` onto
     `ChatCompletion.usage`; `_estimate_tokens` is only the fallback when a build
     reports nothing.
   - Still SafeChain-only: no `cached_tokens`, so skip cache telemetry.
3. If the change is OpenAI-specific (e.g., prompt caching params) → note it as "openai-only" in a comment

Key structural differences:
- `firewall_client` wraps `openai.AsyncOpenAI` directly
- `safechain_client` wraps a LangChain chat model: converts OpenAI-wire messages
  to LangChain messages, binds the SDK's own kwargs via `.bind()`, and converts
  the `AIMessage` back to a `ChatCompletion`. See [[safechain_dual_environment]].
- Only an ALLOW-LIST of sampling kwargs is forwarded on the SafeChain path
  (`max_tokens`, `parallel_tool_calls`, `temperature`, `top_p`, `seed`, `stop`).
  A new knob set in an agent factory reaches OpenAI automatically but must be
  ADDED to that list to reach prod — otherwise it is silently a no-op there.
- Both use `FirewallStack.gate()` for semaphore routing (specialist vs orchestrator pool)
- Both have node trace integration for round-level telemetry

## Documented exception: Amem's own LLM calls (2026-07-31)

`memory/factory.py` passes `client=FirewalledAsyncOpenAI` to
`create_openai_manager` but passes NOTHING equivalent to
`create_safechain_manager`. That asymmetry is intentional — do not "fix" it.

Why it exists at all: this repo calls `aupsert_case_memory` without `content`,
and Amem does `summary = content or await self._asummarize_case(...)`. So every
case consolidation runs an LLM call INSIDE Amem over the case's questions and
answers. On OpenAI that would otherwise go out through a plain `AsyncOpenAI`,
skipping redaction entirely — hence the client.

Why SafeChain is left alone (confirmed by the user, 2026-07-31):
1. **SafeChain's built-in redaction is sufficient for memory writes.** No
   additional v2 redaction pass is required in prod.
2. **Gating is deliberately not shared.** Amem and AgenticSys are independent:
   Amem stores QA/KPs and reloads what it needs. Routing its synthesis through
   `FirewallStack.gate()` would gate end-of-turn consolidation behind live
   specialist capacity for no compliance benefit.
3. **The API shapes differ.** `create_safechain_manager` has no `client`; it
   takes `language_model_factory: Callable[[SafeChainConfig], Any]`, invoked
   PER REQUEST (SafeChain clients expire), returning an object exposing
   `acomplete(prompt, *, purpose, metadata) -> str`. Passing `client=` there is
   a TypeError, not a no-op.

Also in `build_amem_manager`: the `client` kwarg is forwarded only if the
INSTALLED Amem's signature accepts it. Amem and this repo version
independently, and without that check an older Amem raises TypeError into the
broad `except`, silently degrading to `NullAmemManager` — memory stops working
with no obvious cause. `amem_ready` logs `llm_calls_firewalled` so the boot log
says which mode is active.
