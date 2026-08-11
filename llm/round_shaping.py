"""Per-round payload trimming shared by both transports.

One rule today: a round that MUST call a tool cannot emit the final structured
answer, so sending `response_format` on it is dead weight.

WHY IT IS WORTH A MODULE. The SDK computes `response_format` unconditionally —
`OpenAIChatCompletionsModel._fetch_response` has no branch on round number or
tool state — so every round of an agent with an `output_type` carries the full
schema. For the orchestrator that is 8,341 chars of `FinalAnswer` schema on
round 1, whose only job is to emit tool calls under `tool_choice="required"`.

On safechain the cost is not just bytes. The model is a langchain
`AzureChatOpenAI`, and the presence of `response_format` is what routes the call
through OpenAI's AUTO-PARSE path rather than plain `create` — the same
mechanism behind "'make_chart' is not strict. Only 'strict' function can be
auto-parsed". So a bare dispatch round pays for structured-output machinery it
cannot use.

SAFETY. `tool_choice="required"` (or a named-function choice) means the model
must return a tool call, not content. There is no response for the schema to
shape. If the model ignores that and returns prose anyway — a known safechain
flake — the SDK raises `ModelBehaviorError` and the conductor's dispatch-skip
retry handles it. That happens with or without `response_format`, since the
prose would not parse either way; dropping it makes that path no worse.

`auto` / `none` / unset are LEFT ALONE: those rounds may legitimately produce
the final answer, and that is exactly when the schema earns its place.

Applied in BOTH transports, per the openai/safechain parity rule — the openai
path saves the bytes even though it never auto-parses, and keeping the two
behaviours identical is what makes a dev measurement mean anything about prod.
"""
from __future__ import annotations

from typing import Any


def forces_tool_call(tool_choice: Any) -> bool:
    """True when this `tool_choice` obliges the model to emit a tool call.

    `"required"` and a named-function object both do. `"auto"`, `"none"`,
    `None` and the SDK's NOT_GIVEN sentinel do not. Unknown values are treated
    as NOT forcing, so an unrecognised choice keeps the schema rather than
    silently dropping it.
    """
    if isinstance(tool_choice, str):
        return tool_choice == "required"
    # Named-function form: {"type": "function", "function": {"name": …}}
    if isinstance(tool_choice, dict):
        return tool_choice.get("type") == "function"
    return False


def response_format_for_round(tool_choice: Any, response_format: Any) -> Any:
    """`response_format`, or None when this round cannot use it.

    The single decision point for the rule above, so the two transports cannot
    drift apart.
    """
    if response_format is None:
        return None
    return None if forces_tool_call(tool_choice) else response_format
