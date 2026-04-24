"""LLM factory — builds firewall-wrapped LangChain chat models."""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from llm.firewall_stack import FirewallStack, FirewalledModel


def build_llm(
    model_name: str,
    firewall: FirewallStack,
    *,
    api_max_retries: int = 2,
) -> FirewalledModel:
    """Build a LangChain chat model wrapped by the firewall.

    Two retry layers and one concurrency control stack here:
      - `api_max_retries` (this arg) — LangChain's own retry on transient HTTP
        errors (5xx, 429, connection timeouts). Passed through to
        `ChatOpenAI(max_retries=...)`.
      - `firewall.max_retries` (set on FirewallStack)    — content-safety
        retries on `FirewallRejection`. Independent of API retries.
      - `firewall.concurrency_cap` (set on FirewallStack) — caps simultaneous
        LLM requests across ALL wrapped models under this firewall, to
        survive parallel fan-out without hitting provider rate limits.

    Today this returns a `ChatOpenAI`-backed model. Future swaps (Anthropic,
    SafeChain) replace the `ChatOpenAI(...)` line — the call sites stay the same.
    """
    base = ChatOpenAI(model=model_name, max_retries=api_max_retries)
    return firewall.wrap(base)
