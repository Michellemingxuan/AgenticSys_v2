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
2. If yes → apply the same change, adapted for SafeChain's constraints:
   - SafeChain doesn't return `usage` objects → use `_estimate_tokens` (tiktoken)
   - SafeChain doesn't support `cached_tokens` → skip cache telemetry
   - SafeChain flattens messages into a single string → different message format
   - SafeChain has no native tool-calling → tool schemas injected as text
3. If the change is OpenAI-specific (e.g., prompt caching params) → note it as "openai-only" in a comment

Key structural differences:
- `firewall_client` wraps `openai.AsyncOpenAI` → real API responses with usage stats
- `safechain_client` wraps `safechain.core.model` → text-only responses, synthetic ChatCompletion objects
- Both use `FirewallStack.gate()` for semaphore routing (specialist vs orchestrator pool)
- Both have node trace integration for round-level telemetry
