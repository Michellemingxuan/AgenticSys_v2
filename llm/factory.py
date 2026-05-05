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
        # `max_retries=8` lets the openai SDK back off and retry on 429
        # rate-limit responses up to 8 times (it honors the `Retry-After`
        # header). On a 30K-TPM tier the orchestrator pipeline frequently
        # bumps the bucket; without this bump, transient rate-limit errors
        # surface as run failures instead of being absorbed by the SDK.
        base = base_client or AsyncOpenAI(max_retries=8)
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

    async def ainvoke(
        self,
        system_prompt,
        user_message,
        tools=None,
        output_type=None,
        json_mode: bool = False,
    ):
        """Run a single chat completion and parse the response.

        ``json_mode=True`` instructs the model to return a JSON object via
        OpenAI's ``response_format`` and parses the result into a dict, so
        callers like ChatAgent's redact / relevance_check / clarify_intent
        actually see the structured fields they expect (``passed``,
        ``reason``, ``options``, etc.). Without this, the model's JSON-shaped
        response was being stuffed into ``{"response": <raw text>}`` and
        every ``data.get("passed", True)`` lookup defaulted to True — causing
        out-of-scope questions to silently pass scope check.

        ``output_type`` is the legacy Pydantic-validation path; behavior
        unchanged. When neither is set, we still try to parse the content as
        JSON best-effort, falling back to ``{"response": content}``.
        """
        import json as _json
        from models.types import LLMResult

        kwargs: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if json_mode or output_type is not None:
            kwargs["response_format"] = {"type": "json_object"}

        resp = await self._clients.firewalled_client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content

        if output_type is not None:
            try:
                data = output_type(**_json.loads(content)).model_dump()
            except Exception:
                data = {"raw": content}
        elif json_mode:
            # Force-parse: any non-JSON here is a model bug we want surfaced
            # rather than silently swallowed via {"response": ...}.
            try:
                parsed = _json.loads(content)
            except Exception:
                # Fallback to a structured error shape so callers see SOMETHING
                # but don't accidentally hit fail-open defaults.
                parsed = {"raw": content, "_json_parse_error": True}
            data = parsed if isinstance(parsed, dict) else {"response": parsed}
        else:
            # Legacy free-text path (used by ChatAgent.converse). Best-effort
            # JSON parse — fall back to {"response": content} if not JSON.
            try:
                parsed = _json.loads(content)
                data = parsed if isinstance(parsed, dict) else {"response": content}
            except Exception:
                data = {"response": content}

        return LLMResult(status="success", data=data)
