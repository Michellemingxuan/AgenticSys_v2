# Phase 1 — Skill Loader Scaffolding Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a markdown-skill loader (`skills/loader.py`) that parses YAML frontmatter, returns a typed `Skill` object, and offers helpers for filtering by owner and rendering inline prompts. Create empty `skills/workflow/` and `skills/helper/` subdirs for future phases. No existing agent changes.

**Architecture:** `Skill` Pydantic model with required common fields (`name`, `description`, `type`, `owner`, `mode`, `body`) plus a `meta: dict` bucket for type-specific frontmatter (`data_hints`, `inputs`, `outputs`, `tool_signature`, etc.). Loader walks `skills/{workflow,domain,helper}/*.md`, parses frontmatter + body via PyYAML, validates required fields. Helpers: `load_skill(path)`, `load_skills_for(agent_name)`, `render_inline_prompt(skills)`, `helper_tool_specs(skills)`.

**Tech Stack:** Python 3.11+ · Pydantic v2 · PyYAML (already a dep) · pytest.

**Spec:** [docs/specs/2026-04-23-orchestrator-skills-refactor-design.md](../specs/2026-04-23-orchestrator-skills-refactor-design.md) §3

---

## File Structure

**Create:**
- `skills/loader.py` — `Skill` model + parser + filter helpers
- `skills/workflow/.gitkeep` — placeholder so the empty dir lands in git
- `skills/helper/.gitkeep` — placeholder
- `tests/test_skills/test_loader.py` — loader unit tests
- `tests/test_skills/fixtures/` — sample skill `.md` files for tests

**Modify:** none

---

## Task 1: Skill model + single-file loader (`load_skill(path)`)

**Files:**
- Create: `skills/loader.py`
- Create: `tests/test_skills/fixtures/sample_workflow.md`
- Create: `tests/test_skills/test_loader.py`

- [ ] **Step 1: Write the failing test — parse a valid workflow skill**

Create `tests/test_skills/fixtures/sample_workflow.md`:

```markdown
---
name: Sample Workflow
description: A sample workflow skill for testing the loader
type: workflow
owner: [orchestrator]
mode: inline
inputs:
  question: str
outputs:
  answer: str
---

# Purpose

This is the body of the skill. It becomes the system prompt.

# Steps

1. Do the thing.
2. Return an answer.
```

Create `tests/test_skills/test_loader.py`:

```python
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
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_skills/test_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skills.loader'`.

- [ ] **Step 3: Implement `skills/loader.py` skeleton + `load_skill`**

Create `skills/loader.py`:

```python
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
```

- [ ] **Step 4: Run to confirm pass**

Run: `pytest tests/test_skills/test_loader.py::test_load_valid_workflow_skill -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/loader.py tests/test_skills/test_loader.py tests/test_skills/fixtures/sample_workflow.md
git commit -m "feat(skills): add Skill model + single-file loader"
```

---

## Task 2: Validation — reject malformed skill files

**Files:**
- Modify: `tests/test_skills/test_loader.py`
- Create: `tests/test_skills/fixtures/no_frontmatter.md`
- Create: `tests/test_skills/fixtures/missing_required.md`
- Create: `tests/test_skills/fixtures/invalid_yaml.md`

- [ ] **Step 1: Write failing validation tests**

Create `tests/test_skills/fixtures/no_frontmatter.md`:

```markdown
# This file has no frontmatter

Just a body.
```

Create `tests/test_skills/fixtures/missing_required.md`:

```markdown
---
name: Bad skill
description: Missing type, owner, mode
---

Body.
```

Create `tests/test_skills/fixtures/invalid_yaml.md`:

```markdown
---
name: Bad
description: Invalid YAML below
type: workflow
owner: [unclosed list
mode: inline
---

Body.
```

Append to `tests/test_skills/test_loader.py`:

```python
from skills.loader import SkillLoadError


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
```

- [ ] **Step 2: Run tests — expect passes (validation already in place from Task 1)**

Run: `pytest tests/test_skills/test_loader.py -v`
Expected: All 5 tests pass (the 4 new + Task 1).

- [ ] **Step 3: Commit**

```bash
git add tests/test_skills/test_loader.py tests/test_skills/fixtures/
git commit -m "test(skills): loader rejects malformed / missing-field skill files"
```

---

## Task 3: `load_skills_for(agent_name)` — directory scan + owner filter

**Files:**
- Modify: `skills/loader.py`
- Modify: `tests/test_skills/test_loader.py`
- Create: `tests/test_skills/fixtures/sample_helper.md`
- Create: `tests/test_skills/fixtures/shared_skill.md`

- [ ] **Step 1: Add fixtures for different owners**

Create `tests/test_skills/fixtures/sample_helper.md`:

```markdown
---
name: Sample Helper
description: A sample helper skill, tool-callable
type: helper
owner: [chat_agent]
mode: tool
tool_signature: "sample(x: str) -> str"
---

Call this helper when you need to sample something.
```

Create `tests/test_skills/fixtures/shared_skill.md`:

```markdown
---
name: Shared Workflow
description: A skill shared between two agents
type: workflow
owner: [orchestrator, data_manager]
mode: inline
---

This skill is used by both orchestrator and data_manager.
```

- [ ] **Step 2: Write failing tests for directory scan**

Append to `tests/test_skills/test_loader.py`:

```python
from skills.loader import load_skills_for


def test_load_skills_for_filters_by_owner(monkeypatch):
    # Point the loader at our fixtures dir so we don't depend on real skills layout.
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
```

- [ ] **Step 3: Implement `load_skills_for` in `skills/loader.py`**

Append to `skills/loader.py`:

```python
_SKILLS_ROOT = Path(__file__).parent


def load_skills_for(agent_name: str) -> list[Skill]:
    """Return all skills whose `owner` list includes ``agent_name``.

    Walks every `.md` file under `skills/{workflow,domain,helper}/` (and the
    fixtures root used in tests via monkeypatch). Files that fail to parse
    are skipped — parse errors surface in later phases via a stricter
    entrypoint; `load_skills_for` is permissive so one bad skill doesn't
    break the whole agent.
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
```

- [ ] **Step 4: Run the new tests**

Run: `pytest tests/test_skills/test_loader.py -v`
Expected: All 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add skills/loader.py tests/test_skills/test_loader.py tests/test_skills/fixtures/sample_helper.md tests/test_skills/fixtures/shared_skill.md
git commit -m "feat(skills): load_skills_for(agent_name) directory scan + owner filter"
```

---

## Task 4: `render_inline_prompt` + `helper_tool_specs`

**Files:**
- Modify: `skills/loader.py`
- Modify: `tests/test_skills/test_loader.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_skills/test_loader.py`:

```python
from skills.loader import render_inline_prompt, helper_tool_specs


def test_render_inline_prompt_concatenates_bodies(monkeypatch):
    monkeypatch.setattr("skills.loader._SKILLS_ROOT", FIXTURES)

    skills = load_skills_for("orchestrator")
    prompt = render_inline_prompt(skills)

    assert "=== Sample Workflow ===" in prompt
    assert "=== Shared Workflow ===" in prompt
    assert "This is the body" in prompt


def test_render_inline_prompt_skips_tool_mode_skills():
    # Force a mix: inline and tool-mode skills.
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

    # Only the tool-mode helper is returned.
    assert len(specs) == 1
    assert specs[0]["name"] == "Acropedia"
    assert specs[0]["description"] == "Look up abbreviations"
    assert specs[0]["signature"] == "acropedia(term: str) -> dict"
    assert specs[0]["body"] == "Body"
```

- [ ] **Step 2: Implement helpers**

Append to `skills/loader.py`:

```python
def render_inline_prompt(skills: list[Skill]) -> str:
    """Concatenate inline-mode skill bodies into one prompt string.

    Tool-mode skills are skipped — they're exposed to the LLM via tool-calling,
    not injected into the system prompt.
    """
    parts: list[str] = []
    for skill in skills:
        if skill.mode != "inline":
            continue
        parts.append(f"=== {skill.name} ===\n{skill.body}")
    return "\n\n".join(parts)


def helper_tool_specs(skills: list[Skill]) -> list[dict[str, Any]]:
    """Return tool-call specs for tool-mode skills.

    Shape: list of dicts suitable for later adaptation to LangChain `@tool`
    decorators. Inline-mode skills are skipped.
    """
    specs: list[dict[str, Any]] = []
    for skill in skills:
        if skill.mode != "tool":
            continue
        specs.append({
            "name": skill.name,
            "description": skill.description,
            "signature": skill.meta.get("tool_signature", ""),
            "body": skill.body,
        })
    return specs
```

- [ ] **Step 3: Run the new tests**

Run: `pytest tests/test_skills/test_loader.py -v`
Expected: All 10 tests pass.

- [ ] **Step 4: Commit**

```bash
git add skills/loader.py tests/test_skills/test_loader.py
git commit -m "feat(skills): render_inline_prompt + helper_tool_specs"
```

---

## Task 5: Create empty `skills/{workflow,helper}/` subdirs

**Files:**
- Create: `skills/workflow/.gitkeep`
- Create: `skills/helper/.gitkeep`

- [ ] **Step 1: Create dirs with `.gitkeep`**

```bash
touch skills/workflow/.gitkeep
touch skills/helper/.gitkeep
```

- [ ] **Step 2: Verify layout**

```bash
ls skills/
```

Expected:
```
__init__.py
domain/
helper/
loader.py
workflow/
```

- [ ] **Step 3: Run full suite for a green baseline**

Run: `pytest tests/ --tb=short -q`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add skills/workflow/.gitkeep skills/helper/.gitkeep
git commit -m "chore(skills): scaffold empty workflow/ and helper/ subdirs"
```

---

## Out of scope for Phase 1 (future phases)

- Migrating existing inline prompts (`SELECT_TEAM_PROMPT`, domain `*.py` modules) into `.md` skills — Phase 2.
- Wiring any agent to use the loader — Phases 3+.
- LangChain `@tool` adapters for helper skills — Phase 8.
