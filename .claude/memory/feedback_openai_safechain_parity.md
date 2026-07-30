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
