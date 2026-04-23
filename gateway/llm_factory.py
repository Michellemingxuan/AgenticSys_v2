"""LLM factory — builds firewall-wrapped LangChain chat models."""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from gateway.firewall_stack import FirewallStack, FirewalledModel


def build_llm(model_name: str, firewall: FirewallStack) -> FirewalledModel:
    """Build a LangChain chat model wrapped by the firewall.

    Today this returns a `ChatOpenAI`-backed model. Future swaps (Anthropic,
    SafeChain) replace the `ChatOpenAI(...)` line — the call sites stay the same.
    """
    base = ChatOpenAI(model=model_name)
    return firewall.wrap(base)
