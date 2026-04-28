"""Wraps an Agent as a tool with PII redaction on input + output boundaries."""
from __future__ import annotations

from agents import Agent, RunContextWrapper, Runner, function_tool

from llm.firewall_stack import redact_payload, sanitize_message


def redacting_tool(agent: Agent, name: str, description: str):
    """Return a FunctionTool that runs ``agent`` with input/output redaction.

    Inter-agent transit boundary: anything flowing in (LLM-generated sub-
    question) gets ``sanitize_message``; anything flowing out (the inner
    agent's final output) gets ``redact_payload``.
    """
    inner = agent

    @function_tool(name_override=name, description_override=description)
    async def _runner(ctx: RunContextWrapper, sub_question: str) -> str:
        redacted_in = sanitize_message(sub_question)
        result = await Runner.run(inner, redacted_in, context=ctx.context if ctx else None)
        return redact_payload(result.final_output)

    return _runner
