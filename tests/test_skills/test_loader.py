"""Tests for skills.loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.loader import Skill, load_skill


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
