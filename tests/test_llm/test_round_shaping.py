"""A round that MUST call a tool cannot use `response_format`.

The SDK computes it unconditionally — `_fetch_response` has no branch on round
number or tool state — so the orchestrator's dispatch round carries all 8,341
chars of the `FinalAnswer` schema purely to be ignored. On safechain the cost is
not just bytes: the presence of `response_format` is what routes the call
through OpenAI's auto-parse instead of plain `create`.
"""
import pytest

from llm.round_shaping import forces_tool_call, response_format_for_round


_SCHEMA = {"type": "json_schema", "json_schema": {"name": "FinalAnswer"}}


@pytest.mark.parametrize("choice", ["required",
                                    {"type": "function", "function": {"name": "modeling"}}])
def test_a_forced_tool_call_drops_the_schema(choice):
    assert forces_tool_call(choice) is True
    assert response_format_for_round(choice, _SCHEMA) is None


@pytest.mark.parametrize("choice", ["auto", "none", None])
def test_rounds_that_may_answer_keep_the_schema(choice):
    """`auto`/`none`/unset rounds can legitimately produce the final answer —
    exactly when the schema earns its place."""
    assert forces_tool_call(choice) is False
    assert response_format_for_round(choice, _SCHEMA) == _SCHEMA


def test_an_unrecognised_choice_keeps_the_schema():
    """Unknown values must not silently drop structured output."""
    for weird in ("REQUIRED", "any", 7, object(), {"type": "other"}):
        assert forces_tool_call(weird) is False
        assert response_format_for_round(weird, _SCHEMA) == _SCHEMA


def test_no_schema_stays_no_schema():
    assert response_format_for_round("required", None) is None
    assert response_format_for_round("auto", None) is None


def test_safechain_bind_kwargs_applies_the_rule():
    """The transport actually uses it — not just the helper in isolation."""
    from llm.safechain_client import _bind_kwargs

    tools = [{"type": "function", "function": {"name": "modeling"}}]
    dispatch = _bind_kwargs(tools, "required", _SCHEMA)
    assert "response_format" not in dispatch
    assert dispatch["tool_choice"] == "required"      # the round still forces a call

    synth = _bind_kwargs(tools, "auto", _SCHEMA)
    assert synth["response_format"] == _SCHEMA


@pytest.mark.asyncio
async def test_openai_path_applies_the_same_rule():
    """Parity: the openai transport drops it too, so a dev measurement means
    something about prod."""
    from llm.firewall_client import _FirewalledChatCompletions

    seen = {}

    class _Base:
        async def create(self, **kw):
            seen.update(kw)
            return "ok"

    class _Gate:
        def gate(self):
            class _C:
                async def __aenter__(self_inner): return None
                async def __aexit__(self_inner, *a): return False
            return _C()

    c = _FirewalledChatCompletions(_Base(), _Gate())
    await c.create(model="m", messages=[], tool_choice="required",
                   response_format=_SCHEMA, tools=[{"x": 1}])
    assert "response_format" not in seen

    seen.clear()
    await c.create(model="m", messages=[], tool_choice="auto",
                   response_format=_SCHEMA, tools=[{"x": 1}])
    assert seen["response_format"] == _SCHEMA
