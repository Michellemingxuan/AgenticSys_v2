"""The Redact skill's rules about money.

Anchored to a real failure: a reviewer selected a Payment & Spend bullet and
asked "More details about this finding…". The question quoted the report
verbatim, including `$50,200` and `$10,000`, and the redact step returned
`spend: S BERTRAM — $***MASKED*** (2025-05-14)`. The specialists were then
asked about amounts that had been deleted from the question.

The report text was already comma-formatted (that is what
`tools/fs_tools._format_long_numerics` is for), so neither amount was a 6+
CONSECUTIVE-digit run. Redact is an LLM step, and the skill only said "6+-digit
runs" — the model generalised over the separators. These assertions pin the
wording that tells it not to.
"""
from pathlib import Path

from skills.loader import load_skill


def _body() -> str:
    return load_skill(Path("skills/workflow/redact.md")).body


def test_mask_rule_requires_consecutive_digits():
    """"6+-digit runs" was ambiguous about separators; "CONSECUTIVE ... with
    no separator" is not."""
    body = _body()
    assert "6+ CONSECUTIVE digits with no separator" in body


def test_comma_grouped_amounts_are_explicitly_exempt():
    body = _body()
    assert "[1-3 digits],[3 digits]" in body
    # The instruction that actually prevents the failure.
    assert "count only unbroken digits" in body


def test_exemption_is_anchored_to_real_amounts():
    """Per this repo's skill-writing convention, rules carry the concrete
    values that broke, so the model has examples rather than only a rule."""
    body = _body()
    for amount in ("$50,200", "$1,200,700", "174,807.36", "265,000"):
        assert amount in body, f"missing worked example {amount}"


def test_exemption_names_the_upstream_formatter():
    """The comma formatting is deliberate, not incidental — a future editor
    who does not know that might 'simplify' this exemption away."""
    body = _body()
    assert "_format_long_numerics" in body


def test_identifier_masking_is_still_required():
    """The exemption must not have widened into 'never mask long numbers'."""
    body = _body()
    assert "4532123456789" in body
    assert "***MASKED***" in body
    assert "Short digit runs (< 6 digits in a row)" in body
