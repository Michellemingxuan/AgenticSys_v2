"""Wraps an Agent as a tool with PII redaction on input + output boundaries."""
from __future__ import annotations

from agents import Agent, RunContextWrapper, Runner, function_tool

from llm.firewall_stack import redact_payload, sanitize_message


def redacting_tool(agent: Agent, name: str, description: str):
    """Return a FunctionTool that runs ``agent`` with input/output redaction.

    Inter-agent transit boundary: anything flowing in (LLM-generated sub-
    question) gets ``sanitize_message``; anything flowing out (the inner
    agent's final output) gets ``redact_payload``.

    Multi-turn behavior: when ``ctx.context`` carries a
    ``_specialist_histories`` dict (see ``AppContext``), this wrapper reads
    the entry keyed by ``name`` to find the specialist's prior conversation
    and prepends it to the new sub-question on each call. After the run,
    the updated history (``result.to_input_list()``) is saved back. So a
    follow-up tool call to the same specialist within the same AppContext
    sees what the specialist already asked / answered, instead of starting
    fresh. Reset by constructing a new AppContext.
    """
    inner = agent

    @function_tool(name_override=name, description_override=description)
    async def _runner(ctx: RunContextWrapper, sub_question: str) -> str:
        redacted_in = sanitize_message(sub_question)

        # Look up per-specialist history on the surrounding AppContext.
        # When the context doesn't expose `_specialist_histories` (e.g.
        # tests with a bare context object), behave like the legacy
        # single-turn path.
        app_ctx = ctx.context if ctx else None
        histories = getattr(app_ctx, "_specialist_histories", None)
        prior = histories.get(name) if isinstance(histories, dict) else None

        if prior:
            run_input = prior + [{"role": "user", "content": redacted_in}]
        else:
            run_input = redacted_in

        result = await Runner.run(inner, run_input, context=app_ctx)

        # Persist the updated history so the next call to this specialist
        # in the same context picks up where we left off.
        if isinstance(histories, dict) and hasattr(result, "to_input_list"):
            histories[name] = result.to_input_list()

        return redact_payload(result.final_output)

    return _runner
