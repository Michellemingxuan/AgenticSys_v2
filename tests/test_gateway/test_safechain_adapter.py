"""Tests for SafeChainAdapter pre-sanitization."""

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
