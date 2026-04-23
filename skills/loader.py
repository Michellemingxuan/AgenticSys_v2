"""Markdown-skill loader — parses YAML frontmatter + body from `.md` files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


SkillType = Literal["workflow", "domain", "helper"]
SkillMode = Literal["inline", "tool"]


class Skill(BaseModel):
    """A markdown-authored skill — frontmatter + body.

    Common fields are typed; type-specific extras live in `meta`.
    """

    name: str
    description: str
    type: SkillType
    owner: list[str]
    mode: SkillMode
    body: str
    meta: dict[str, Any] = Field(default_factory=dict)
    path: Path | None = None


class SkillLoadError(Exception):
    """Raised when a skill file is missing or malformed."""


def load_skill(path: str | Path) -> Skill:
    """Parse a single skill file into a `Skill`.

    Raises `SkillLoadError` if the file is missing, the frontmatter is absent,
    the YAML is invalid, or required fields are missing.
    """
    path = Path(path)
    if not path.exists():
        raise SkillLoadError(f"Skill file not found: {path}")

    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillLoadError(f"No YAML frontmatter found in {path}")

    try:
        fm = yaml.safe_load(match.group("frontmatter")) or {}
    except yaml.YAMLError as e:
        raise SkillLoadError(f"Malformed YAML frontmatter in {path}: {e}") from e

    if not isinstance(fm, dict):
        raise SkillLoadError(f"Frontmatter must be a mapping in {path}")

    # Pull known fields out; everything else goes into meta.
    reserved = {"name", "description", "type", "owner", "mode"}
    common = {k: fm[k] for k in reserved if k in fm}
    meta = {k: v for k, v in fm.items() if k not in reserved}

    try:
        return Skill(
            **common,
            body=match.group("body").strip(),
            meta=meta,
            path=path,
        )
    except Exception as e:  # pydantic ValidationError, missing required, etc.
        raise SkillLoadError(f"Invalid skill frontmatter in {path}: {e}") from e


_SKILLS_ROOT = Path(__file__).parent


def load_skills_for(agent_name: str) -> list[Skill]:
    """Return all skills whose `owner` list includes ``agent_name``.

    Walks every `.md` file under `skills/{workflow,domain,helper}/`. Files
    that fail to parse are skipped — parse errors surface via the stricter
    `load_skill` entrypoint; `load_skills_for` is permissive so one bad
    skill doesn't break the whole agent.
    """
    skills: list[Skill] = []
    for md_path in sorted(_SKILLS_ROOT.rglob("*.md")):
        try:
            skill = load_skill(md_path)
        except SkillLoadError:
            continue
        if agent_name in skill.owner:
            skills.append(skill)
    return skills
