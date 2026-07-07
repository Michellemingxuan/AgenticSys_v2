"""Guards that the report_needle skill still parses and carries the grep path."""
from pathlib import Path

from skills.loader import load_skill

_SKILL = Path(__file__).parent.parent.parent / "skills" / "workflow" / "report_needle.md"


def test_report_needle_parses_and_stays_inline():
    skill = load_skill(_SKILL)
    assert skill.mode == "inline"
    # frontmatter inputs/outputs preserved in meta
    assert "question" in skill.meta.get("inputs", {})
    assert "coverage" in skill.meta.get("outputs", {})


def test_report_needle_documents_grep_path():
    body = load_skill(_SKILL).body
    # the new consolidated-file path is described
    assert "fs_grep" in body
    assert "start_line" in body and "end_line" in body
    # table routing is still present (multi-file primary path)
    assert "Concept → file" in body
    # grep is discovery-only
    assert "DISCOVERY ONLY" in body
