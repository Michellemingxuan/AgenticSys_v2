# tests/test_server/test_plan_review_dispatch.py
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "skills" / "workflow"


def test_team_construction_skill_content():
    body = (SKILLS / "team_construction.md").read_text(encoding="utf-8")
    # dispatch-shape guidance present
    assert "Dispatch shape" in body
    for shape in ("parallel", "collapse", "sequential"):
        assert shape in body.lower()
    # row-31 restriction removed
    assert "NOT TSR/CDSS" not in body
