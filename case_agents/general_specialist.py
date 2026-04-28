"""General Specialist — cross-domain reviewer with Compare skill."""

from __future__ import annotations

from pathlib import Path

from agents import Agent
from models.types import ReviewReport
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


COMPARE_SYSTEM_PROMPT = _load_skill(_WORKFLOW_DIR / "comparison.md").body


def build_general_specialist(model) -> Agent:
    return Agent(
        name="general_specialist",
        instructions=COMPARE_SYSTEM_PROMPT,
        tools=[],
        output_type=ReviewReport,
        model=model,
    )
