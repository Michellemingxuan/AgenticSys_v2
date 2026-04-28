"""LLM factory — builds firewall-wrapped LangChain chat models and SDK session clients."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from agents import OpenAIChatCompletionsModel

from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack, FirewalledModel


@dataclass
class SessionClients:
    firewalled_client: FirewalledAsyncOpenAI
    model: OpenAIChatCompletionsModel


def build_session_clients(
    firewall: FirewallStack,
    *,
    model_name: str = "gpt-4o",
    base_client: AsyncOpenAI | None = None,
) -> SessionClients:
    """Build a firewalled AsyncOpenAI client and the SDK Model wrapping it.

    This is the new-path factory used by the OpenAI Agents SDK migration.
    The legacy ``build_llm`` function is kept alongside for backward compat
    until Phase 6 / Task 6.4 removes LangChain entirely.
    """
    base = base_client or AsyncOpenAI()
    firewalled = FirewalledAsyncOpenAI(base=base, firewall=firewall)
    model = OpenAIChatCompletionsModel(model=model_name, openai_client=firewalled)
    return SessionClients(firewalled_client=firewalled, model=model)


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
