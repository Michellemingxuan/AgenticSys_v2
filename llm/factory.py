"""LLM factory — builds firewalled SDK session clients."""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI

from agents import OpenAIChatCompletionsModel

from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack


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
    """Build a firewalled AsyncOpenAI client and the SDK Model wrapping it."""
    base = base_client or AsyncOpenAI()
    firewalled = FirewalledAsyncOpenAI(base=base, firewall=firewall)
    model = OpenAIChatCompletionsModel(model=model_name, openai_client=firewalled)
    return SessionClients(firewalled_client=firewalled, model=model)



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
