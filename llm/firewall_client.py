"""FirewalledAsyncOpenAI — wraps openai.AsyncOpenAI with PII redaction,
retry-with-guidance on FirewallRejection, and a shared concurrency cap."""

from __future__ import annotations

from typing import Any

from llm.firewall_stack import FIREWALL_GUIDANCE, FirewallRejection, FirewallStack, sanitize_message


def _redact_message(message: dict) -> dict:
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": sanitize_message(content)}
    return message


def _inject_guidance(messages: list[dict]) -> list[dict]:
    """Append firewall guidance to the system message; resanitize all messages."""
    out = []
    appended = False
    for m in messages:
        m = _redact_message(m)
        if not appended and m.get("role") == "system":
            m = {**m, "content": (m.get("content") or "") + "\n\n" + FIREWALL_GUIDANCE}
            appended = True
        out.append(m)
    return out


class _FirewalledChatCompletions:
    def __init__(self, base_completions: Any, firewall: FirewallStack):
        self._base = base_completions
        self._firewall = firewall

    async def create(self, *, model, messages, **kw):
        messages = [_redact_message(m) for m in messages]
        attempt = 0
        while True:
            try:
                return await self._base.create(model=model, messages=messages, **kw)
            except FirewallRejection as e:
                self._firewall.logger.log("firewall_rejection",
                                          {"code": e.code, "message": e.message,
                                           "attempt": attempt})
                if attempt >= self._firewall.max_retries:
                    self._firewall.logger.log("firewall_blocked",
                                              {"code": e.code, "message": e.message,
                                               "attempts": attempt + 1})
                    raise
                attempt += 1
                messages = _inject_guidance(messages)


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
