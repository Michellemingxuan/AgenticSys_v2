"""Tests for skills.loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.loader import (
    Skill,
    SkillLoadError,
    helper_tool_specs,
    load_skill,
    load_skills_for,
    render_inline_prompt,
)


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


def test_load_skills_for_filters_by_owner(monkeypatch):
    monkeypatch.setattr("skills.loader._SKILLS_ROOT", FIXTURES)

    orch_skills = load_skills_for("orchestrator")
    names = {s.name for s in orch_skills}
    assert "Sample Workflow" in names
    assert "Shared Workflow" in names
    assert "Sample Helper" not in names  # owned by chat_agent


def test_load_skills_for_shared_skill_returned_for_both_owners(monkeypatch):
    monkeypatch.setattr("skills.loader._SKILLS_ROOT", FIXTURES)

    dm_skills = load_skills_for("data_manager")
    names = {s.name for s in dm_skills}
    assert "Shared Workflow" in names


def test_render_inline_prompt_concatenates_bodies(monkeypatch):
    monkeypatch.setattr("skills.loader._SKILLS_ROOT", FIXTURES)

    skills = load_skills_for("orchestrator")
    prompt = render_inline_prompt(skills)

    assert "=== Sample Workflow ===" in prompt
    assert "=== Shared Workflow ===" in prompt
    assert "This is the body" in prompt


def test_render_inline_prompt_skips_tool_mode_skills():
    inline = Skill(
        name="A", description="a", type="workflow", owner=["x"], mode="inline", body="BODY-A"
    )
    tool = Skill(
        name="B", description="b", type="helper", owner=["x"], mode="tool", body="BODY-B"
    )
    prompt = render_inline_prompt([inline, tool])
    assert "BODY-A" in prompt
    assert "BODY-B" not in prompt


def test_helper_tool_specs_returns_tool_shaped_dicts():
    helper = Skill(
        name="Acropedia",
        description="Look up abbreviations",
        type="helper",
        owner=["chat_agent"],
        mode="tool",
        body="Body",
        meta={"tool_signature": "acropedia(term: str) -> dict"},
    )
    inline = Skill(
        name="Team Construction",
        description="pick team",
        type="workflow",
        owner=["orchestrator"],
        mode="inline",
        body="...",
    )
    specs = helper_tool_specs([helper, inline])

    assert len(specs) == 1
    assert specs[0]["name"] == "Acropedia"
    assert specs[0]["description"] == "Look up abbreviations"
    assert specs[0]["signature"] == "acropedia(term: str) -> dict"
    assert specs[0]["body"] == "Body"
