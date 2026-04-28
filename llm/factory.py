"""LLM factory — builds firewalled SDK session clients.

Two backends supported:

- ``backend="openai"`` (default, dev/test) — wraps ``openai.AsyncOpenAI`` with
  :class:`llm.firewall_client.FirewalledAsyncOpenAI`.
- ``backend="safechain"`` (private/prod) — uses
  :class:`llm.safechain_client.SafeChainAsyncOpenAI`, which mimics the
  AsyncOpenAI shape but routes ``chat.completions.create`` through SafeChain.

The agent architecture downstream (Agent, Runner, redacting_tool, β fallback)
is *identical* in both cases — only the HTTP client differs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from openai import AsyncOpenAI

from agents import OpenAIChatCompletionsModel

from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack


Backend = Literal["openai", "safechain"]


@dataclass
class SessionClients:
    firewalled_client: Any  # FirewalledAsyncOpenAI or SafeChainAsyncOpenAI
    model: OpenAIChatCompletionsModel
    backend: Backend = "openai"


def build_session_clients(
    firewall: FirewallStack,
    *,
    model_name: str = "gpt-4o",
    base_client: AsyncOpenAI | None = None,
    backend: Backend | None = None,
) -> SessionClients:
    """Build a firewalled HTTP-client + SDK Model wrapping it.

    ``backend`` selects the LLM transport:

    - If unset, falls back to the ``LLM_BACKEND`` env var, then to ``"openai"``.
    - ``"openai"`` builds a :class:`FirewalledAsyncOpenAI` over either
      ``base_client`` or a fresh ``AsyncOpenAI()``.
    - ``"safechain"`` builds a :class:`SafeChainAsyncOpenAI`. ``base_client``
      is ignored; ``safechain.lcel.model(...)`` is loaded lazily on first use.
    """
    if backend is None:
        backend = os.environ.get("LLM_BACKEND", "openai")  # type: ignore[assignment]

    if backend == "safechain":
        # Lazy import — safechain is unavailable in the dev env, but we still
        # want this module to import successfully for tests.
        from llm.safechain_client import SafeChainAsyncOpenAI

        client: Any = SafeChainAsyncOpenAI(model_name=model_name, firewall=firewall)
    elif backend == "openai":
        base = base_client or AsyncOpenAI()
        client = FirewalledAsyncOpenAI(base=base, firewall=firewall)
    else:
        raise ValueError(
            f"Unknown LLM backend {backend!r}. Use 'openai' or 'safechain'."
        )

    model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
    return SessionClients(firewalled_client=client, model=model, backend=backend)



class FirewalledChatShim:
    """Minimal LLMResult-style shim for ChatAgent's existing call sites.

    Preserves the legacy ``llm.ainvoke(system_prompt, user_message, tools=, output_type=)
    -> LLMResult`` surface so ``ChatAgent`` (which makes short single-turn LLM
    calls for screening/redacting/conversing) keeps working without a rewrite.
    Migrating ChatAgent to be an SDK ``Agent`` is out of scope (spec §8).

    The ``tools=`` parameter is accepted for signature compatibility but ignored —
    ChatAgent's screen/redact/relevance/converse calls pass tools for follow-up Q&A
    but the shim delegates to plain chat completions. If real tool execution is
    needed in future, ChatAgent should be rewritten as an SDK Agent.
    """

    def __init__(self, clients: SessionClients, model_name: str | None = None):
        self._clients = clients
        # Use the same model the SDK Agents are using by default
        self._model = model_name or clients.model.model
        self.firewall = clients.firewalled_client._firewall  # legacy access

    async def ainvoke(self, system_prompt, user_message, tools=None, output_type=None):
        from models.types import LLMResult
        resp = await self._clients.firewalled_client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        content = resp.choices[0].message.content
        if output_type is not None:
            try:
                import json as _json
                data = output_type(**_json.loads(content)).model_dump()
            except Exception:
                data = {"raw": content}
        else:
            data = {"response": content}
        return LLMResult(status="success", data=data)
