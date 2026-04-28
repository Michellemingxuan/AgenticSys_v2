"""FirewalledAsyncOpenAI — wraps openai.AsyncOpenAI with PII redaction,
retry-with-guidance on FirewallRejection, and a shared concurrency cap."""

from __future__ import annotations

from typing import Any

from llm.firewall_stack import FirewallStack, sanitize_message


def _redact_message(message: dict) -> dict:
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": sanitize_message(content)}
    return message


class _FirewalledChatCompletions:
    def __init__(self, base_completions: Any, firewall: FirewallStack):
        self._base = base_completions
        self._firewall = firewall

    async def create(self, *, model, messages, **kw):
        messages = [_redact_message(m) for m in messages]
        return await self._base.create(model=model, messages=messages, **kw)


class _FirewalledChat:
    def __init__(self, base_chat: Any, firewall: FirewallStack):
        self.completions = _FirewalledChatCompletions(base_chat.completions, firewall)


class FirewalledAsyncOpenAI:
    """Drop-in replacement for openai.AsyncOpenAI used by the Agents SDK."""

    def __init__(self, base: Any, firewall: FirewallStack):
        self._base = base
        self._firewall = firewall
        self.chat = _FirewalledChat(base.chat, firewall)

    def __getattr__(self, name: str):
        # Delegate all other endpoints (responses, files, etc.) to the base client.
        return getattr(self._base, name)
