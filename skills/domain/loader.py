"""Domain skill loader — reads markdown skill files and adapts them to DomainSkill."""

from __future__ import annotations

from pathlib import Path

from models.types import DomainSkill
from skills.loader import SkillLoadError, load_skill


_DOMAIN_DIR = Path(__file__).parent


def load_domain_skill(name: str) -> DomainSkill | None:
    """Load a domain skill by name, returning the legacy `DomainSkill` dataclass.

    Reads `skills/domain/<name>.md`. The markdown's frontmatter carries the
    structured fields (`data_hints`, `interpretation_guide`, `risk_signals`)
    and the body carries the `system_prompt` prose.

    Returns None if the file is missing or fails to parse — matches the
    previous behavior of the Python-module-based loader.
    """
    md_path = _DOMAIN_DIR / f"{name}.md"
    if not md_path.exists():
        return None

    try:
        skill = load_skill(md_path)
    except SkillLoadError:
        return None

    meta = skill.meta
    return DomainSkill(
        name=skill.name,
        system_prompt=skill.body,
        data_hints=list(meta.get("data_hints", [])),
        interpretation_guide=str(meta.get("interpretation_guide", "")).strip(),
        risk_signals=list(meta.get("risk_signals", [])),
    )


def list_domain_skills() -> list[str]:
    """Return the sorted list of domain skill names (filename stems)."""
    return sorted(p.stem for p in _DOMAIN_DIR.glob("*.md"))
