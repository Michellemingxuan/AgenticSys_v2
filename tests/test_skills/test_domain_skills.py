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
