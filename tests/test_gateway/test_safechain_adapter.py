"""Tests for SafeChainAdapter pre-sanitization."""

import sys
import types

from gateway.safechain_adapter import SafeChainAdapter


def test_pre_sanitize_masks_case_token():
    sample = "Context:\nTables for CASE-00001.\n\nRequest:\nAnalyze payments."
    cleaned = SafeChainAdapter._pre_sanitize(sample)
    assert "CASE-00001" not in cleaned
    assert "<case>" in cleaned


def test_pre_sanitize_masks_long_digit_runs():
    cleaned = SafeChainAdapter._pre_sanitize("account 12345678901234")
    assert "12345678901234" not in cleaned
    assert "***MASKED***" in cleaned


def test_pre_sanitize_filters_exec_keywords():
    cleaned = SafeChainAdapter._pre_sanitize("please exec this")
    assert "[FILTERED]" in cleaned


def test_pre_sanitize_preserves_benign_text():
    sample = "Nothing sensitive, just a short note."
    assert SafeChainAdapter._pre_sanitize(sample) == sample


def test_invoke_runs_pre_sanitize_on_combined_prompt(monkeypatch):
    """Integration test: SafeChainAdapter._invoke must run _pre_sanitize on the combined prompt
    before the LLM sees it. Uses monkey-patching so the test does not require the real SafeChain
    library or network access."""
    # Stub safechain.prompts.ValidChatPromptTemplate with a minimal drop-in.
    fake_safechain = types.ModuleType("safechain")
    fake_prompts = types.ModuleType("safechain.prompts")

    class _FakeTemplate:
        def __init__(self, messages):
            self._messages = messages

        @classmethod
        def from_messages(cls, messages):
            return cls(messages)

        def __or__(self, other):
            # Chain: template | llm — return an object whose .invoke(vars) returns a result object
            class _Chain:
                def __init__(self, tmpl, llm):
                    self._tmpl = tmpl
                    self._llm = llm

                def invoke(self, vars):
                    # Capture what actually reached the LLM for assertions.
                    captured["final"] = vars["__input__"]

                    class _Result:
                        content = '{"output": {"ok": true}}'
                    return _Result()
            return _Chain(self, other)

    fake_prompts.ValidChatPromptTemplate = _FakeTemplate
    fake_safechain.prompts = fake_prompts
    monkeypatch.setitem(sys.modules, "safechain", fake_safechain)
    monkeypatch.setitem(sys.modules, "safechain.prompts", fake_prompts)

    # Dummy LLM — the chain only uses it via __or__, never calls it.
    captured = {}

    class _DummyLLM:
        pass

    adapter = SafeChainAdapter(llm=_DummyLLM(), model_name="test-model")
    adapter.run(
        system_prompt="Analyze CASE-00001",
        user_message="See CASE-00002 for context",
    )

    final = captured["final"]
    # The combined prompt that reached the LLM must not contain raw case IDs.
    assert "CASE-00001" not in final
    assert "CASE-00002" not in final
    assert "<case>" in final
