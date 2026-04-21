"""Tests for SafeChainAdapter pre-sanitization."""

import sys
import types

from gateway.safechain_adapter import SafeChainAdapter


def test_pre_sanitize_masks_active_case_id():
    sample = "Context: Tables for 77165907010. Request: Analyze payments."
    cleaned = SafeChainAdapter._pre_sanitize(sample, case_id="77165907010")
    assert "77165907010" not in cleaned
    assert "<case>" in cleaned


def test_pre_sanitize_masks_long_digit_runs():
    # An 8-digit run that is NOT the active case is still masked by the digit rule.
    cleaned = SafeChainAdapter._pre_sanitize("account 12345678", case_id=None)
    assert "12345678" not in cleaned
    assert "***MASKED***" in cleaned


def test_pre_sanitize_scrubber_runs_before_digit_mask():
    # The 11-digit case ID must become <case>, NOT ***MASKED*** — proves ordering.
    cleaned = SafeChainAdapter._pre_sanitize("id 77165907010 here", case_id="77165907010")
    assert "<case>" in cleaned
    assert "***MASKED***" not in cleaned


def test_pre_sanitize_filters_exec_keywords():
    cleaned = SafeChainAdapter._pre_sanitize("please exec this", case_id=None)
    assert "[FILTERED]" in cleaned


def test_pre_sanitize_preserves_benign_text():
    sample = "Nothing sensitive, just a short note."
    assert SafeChainAdapter._pre_sanitize(sample, case_id=None) == sample


def test_invoke_runs_pre_sanitize_on_combined_prompt(monkeypatch):
    """Integration test: _invoke must apply _pre_sanitize (including case scrubbing)
    to the combined prompt before the LLM sees it. Mocks safechain.prompts so the
    test does not require the real SafeChain library."""
    fake_safechain = types.ModuleType("safechain")
    fake_prompts = types.ModuleType("safechain.prompts")

    captured = {}

    class _FakeTemplate:
        def __init__(self, messages):
            self._messages = messages

        @classmethod
        def from_messages(cls, messages):
            return cls(messages)

        def __or__(self, other):
            class _Chain:
                def __init__(self, tmpl, llm):
                    self._tmpl = tmpl
                    self._llm = llm

                def invoke(self, vars):
                    captured["final"] = vars["__input__"]

                    class _Result:
                        content = '{"output": {"ok": true}}'
                    return _Result()
            return _Chain(self, other)

    fake_prompts.ValidChatPromptTemplate = _FakeTemplate
    fake_safechain.prompts = fake_prompts
    monkeypatch.setitem(sys.modules, "safechain", fake_safechain)
    monkeypatch.setitem(sys.modules, "safechain.prompts", fake_prompts)

    class _DummyGateway:
        def get_case_id(self):
            return "77165907010"

    class _DummyLLM:
        pass

    adapter = SafeChainAdapter(llm=_DummyLLM(), model_name="test-model", gateway=_DummyGateway())
    adapter.run(
        system_prompt="Analyze the case 77165907010",
        user_message="See 77165907010 for details",
    )

    final = captured["final"]
    assert "77165907010" not in final
    assert "<case>" in final
