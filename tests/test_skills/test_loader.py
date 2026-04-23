"""Tests for skills.loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.loader import Skill, SkillLoadError, load_skill


FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_workflow_skill():
    skill = load_skill(FIXTURES / "sample_workflow.md")

    assert isinstance(skill, Skill)
    assert skill.name == "Sample Workflow"
    assert skill.type == "workflow"
    assert skill.owner == ["orchestrator"]
    assert skill.mode == "inline"
    assert "This is the body" in skill.body
    assert skill.meta["inputs"] == {"question": "str"}
    assert skill.meta["outputs"] == {"answer": "str"}


def test_load_skill_missing_file_raises():
    with pytest.raises(SkillLoadError, match="not found"):
        load_skill(FIXTURES / "does_not_exist.md")


def test_load_skill_no_frontmatter_raises():
    with pytest.raises(SkillLoadError, match="No YAML frontmatter"):
        load_skill(FIXTURES / "no_frontmatter.md")


def test_load_skill_missing_required_fields_raises():
    with pytest.raises(SkillLoadError, match="Invalid skill frontmatter"):
        load_skill(FIXTURES / "missing_required.md")


def test_load_skill_malformed_yaml_raises():
    with pytest.raises(SkillLoadError, match="Malformed YAML"):
        load_skill(FIXTURES / "invalid_yaml.md")
