"""Tests for domain skills and loader."""

import pytest

from skills.domain.loader import load_domain_skill, list_domain_skills


def test_load_bureau_skill():
    skill = load_domain_skill("bureau")
    assert skill is not None
    assert skill.name == "bureau"
    assert len(skill.data_hints) > 0
    assert len(skill.risk_signals) > 0


def test_load_all_domain_skills():
    names = list_domain_skills()
    # Currently 8: bureau, capacity_afford, crossbu, customer_rel, modeling,
    # spend_payments, strategy, wcc. Bump this count when a new
    # `skills/domain/*.md` skill lands.
    assert len(names) == 8
    for name in names:
        skill = load_domain_skill(name)
        assert skill is not None, f"Failed to load skill: {name}"


def test_load_nonexistent_skill():
    assert load_domain_skill("does_not_exist") is None


def test_all_skills_have_required_fields():
    for name in list_domain_skills():
        skill = load_domain_skill(name)
        assert skill.system_prompt, f"{name} missing system_prompt"
        assert skill.data_hints, f"{name} missing data_hints"
        assert skill.interpretation_guide, f"{name} missing interpretation_guide"
        assert skill.risk_signals, f"{name} missing risk_signals"


def test_modeling_defers_to_directed_variables():
    from skills.domain.loader import load_domain_skill
    body = load_domain_skill("modeling").system_prompt
    assert "DIRECTED VARIABLES" in body
    assert "Out-of-pattern (OOP)" in body  # semantic overlay retained


def test_data_query_forbids_attaching_a_monthly_score_to_a_transaction():
    """Reported: *"summarize the abnormal transactions"* tabled a $20,500 spend
    dated 2025-04-24 with "TSR Score (monthly max) = 26.4 (May'25)".

    Two compounded errors. The GRAIN — that case carries model scores monthly
    only, so the transaction has no score and a monthly maximum belongs to the
    month, not to any one transaction. And within that grain the MONTH — 26.4
    is the maximum over the Feb-May window; April's own figure is 21.6.

    The specialist labelled the column "(monthly max)" itself, so it knew what
    the number was and asserted it anyway. That makes this a rule about what
    may be CLAIMED, which is why it lives in the skill rather than in a tool.
    """
    from pathlib import Path
    from skills.loader import load_skill
    body = load_skill(
        Path("skills/workflow/data_query.md")).body

    assert "A MONTHLY score is never a transaction's score" in body
    # the two prohibitions, and the honest alternative
    assert "Never put a monthly figure in a per-transaction row" in body
    assert "Never carry a window aggregate across months" in body
    assert "`data_gaps`" in body
    # anchored to the real failure, per this repo's skill-writing convention
    assert "2025-04-24" in body and "26.4" in body and "21.6" in body
