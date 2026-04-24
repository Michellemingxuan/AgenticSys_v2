# Data Layer Rename + Conceptual Pull Signal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the ambiguous `data/` and `gateway/` Python packages, reshape `data_tables/` into `{simulated,real}/` subfolders, add a `--data-source` selection flag with fallback to the generator, and wire a structured "insufficient data — would pull" advisory signal into the Balance step that surfaces through the chat formatter.

**Architecture:** Nine mechanical tasks, each ending in a commit. Five are pure renames + import updates (packages, class, folders). Four wire the new pull-signal feature end-to-end: new type → skill paragraph → orchestrator parsing → chat formatter rendering.

**Tech Stack:** Python 3.11, pydantic, pytest, existing modules (`data/` → `datalayer/`, `gateway/` → `llm/`, `orchestrator/orchestrator.py`, `orchestrator/chat_agent.py`, `models/types.py`, `skills/workflow/balancing.md`, `main.py`).

**Spec reference:** `docs/specs/2026-04-25-data-layer-rename-and-pull-signal-design.md`.

---

## Pre-flight notes for the implementer

- Repo is on `main` branch. No worktree. Commits go directly on `main` per existing project convention.
- `git mv` is preferred for directory renames so history is preserved, but a plain `mv` + `git add` + `git rm` also works.
- The project has four recently-built iteration notebooks (`notebooks/test_team_construction.ipynb`, `test_report_agent.ipynb`, `test_compare_review.ipynb`, `test_data_query.ipynb`) with `from data.*` and `from gateway.*` imports inside code cells. These need updating in Tasks 1 and 2. Use `nbformat` programmatically (a tiny Python script) rather than hand-editing .ipynb JSON.
- **Test directories also rename:** `tests/test_data/` → `tests/test_datalayer/` and `tests/test_gateway/` → `tests/test_llm/`. Fold into Tasks 1 and 2 so imports and directory names stay consistent.
- Run `pytest` after each rename task to confirm green. If a test fails for reasons unrelated to the rename, fix only the import breakage and flag the unrelated failure in the report.
- Do NOT use `--no-verify` on commits. If a pre-commit hook fails, fix the underlying issue and recommit.
- `sed -i ''` (empty-string backup suffix) is the BSD/macOS form used below. On Linux, use `sed -i` without the empty string. The project runs on macOS (Darwin), so commands are written that way.

### Full list of import sites to update (approximate)

**`from data.*` → `from datalayer.*`** (Task 1):
- `main.py` (3 imports), `tools/data_tools.py` (2), `agents/data_manager_agent.py` (3 incl. `from data import adapter`), `tests/test_catalog_sync.py`, `tests/test_adapter.py` (many), `tests/test_agents/test_data_manager_agent.py` (3), `tests/test_tools/test_data_tools.py` (2), `tests/test_data/test_gateway.py` (3), `tests/test_data/test_generator.py` (multi), `tests/test_e2e/test_smoke.py` (3), plus the four new notebooks under `notebooks/`.

**`from gateway.*` → `from llm.*`** (Task 2):
- `main.py` (2), `agents/base_agent.py` (1), `agents/data_manager_agent.py` (1), `agents/report_agent.py` (1), `agents/general_specialist.py` (1), `orchestrator/orchestrator.py` (1), `orchestrator/chat_agent.py` (1), `tests/test_orchestrator/test_orchestrator.py` (1), `tests/test_gateway/test_firewalled_model.py`, `test_llm_factory.py`, `test_case_scrubber.py`, plus the four notebooks.

Grep before and after each task to confirm zero stale imports remain:

```bash
grep -rn "from data\." --include="*.py" .
grep -rn "from gateway\." --include="*.py" .
```

---

## Task 1: Rename Python package `data/` → `datalayer/`

**Files:**
- Rename: `data/` → `datalayer/`
- Rename: `tests/test_data/` → `tests/test_datalayer/`
- Modify: every file matching `grep -rn "from data\." --include="*.py" .` (see pre-flight)
- Modify: four notebook code cells under `notebooks/test_*.ipynb`

- [ ] **Step 1: Perform the directory renames with git mv**

```bash
cd <repo-root>
git mv data datalayer
git mv tests/test_data tests/test_datalayer
```

- [ ] **Step 2: Update all `from data.` imports**

Across the repo, replace every `from data.` with `from datalayer.` and every `import data` / `from data import X` with the `datalayer` equivalent. Simplest:

```bash
# Python source files
grep -rln "from data\." --include="*.py" . | xargs sed -i '' 's/from data\./from datalayer./g'
grep -rln "from data import" --include="*.py" . | xargs sed -i '' 's/from data import/from datalayer import/g'
```

Verify zero stale imports remain:

```bash
grep -rn "from data\." --include="*.py" .
grep -rn "from data import" --include="*.py" .
```

Both commands should return no matches.

- [ ] **Step 3: Update the four iteration notebooks**

Notebook code cells contain `from data.catalog import ...`, `from data.gateway import ...`, `from data.generator import ...`. Use `nbformat` to update them:

```bash
python3 <<'EOF'
import nbformat
from pathlib import Path

notebooks = [
    "notebooks/test_team_construction.ipynb",
    "notebooks/test_report_agent.ipynb",
    "notebooks/test_compare_review.ipynb",
    "notebooks/test_data_query.ipynb",
]
for nb_path in notebooks:
    nb = nbformat.read(nb_path, as_version=4)
    changed = False
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        if "from data." in cell.source or "from data import" in cell.source:
            cell.source = cell.source.replace("from data.", "from datalayer.")
            cell.source = cell.source.replace("from data import", "from datalayer import")
            changed = True
    if changed:
        nbformat.write(nb, nb_path)
        print(f"Updated {nb_path}")
    else:
        print(f"(no data imports in {nb_path})")
EOF
```

- [ ] **Step 4: Run the test suite to confirm green**

```bash
pytest
```

Expected: all tests that were passing before still pass. No `ImportError` or `ModuleNotFoundError` on `data`. If a test fails for a non-import reason, note it in the report but do not fix unrelated failures in this task.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: rename data/ package to datalayer/"
```

---

## Task 2: Rename Python package `gateway/` → `llm/`, `llm_factory.py` → `factory.py`

**Files:**
- Rename: `gateway/` → `llm/`
- Rename: `gateway/llm_factory.py` → `llm/factory.py`
- Rename: `tests/test_gateway/` → `tests/test_llm/`
- Rename: `tests/test_gateway/test_llm_factory.py` → `tests/test_llm/test_factory.py`
- Modify: every file matching `grep -rn "from gateway\." --include="*.py" .`
- Modify: four notebook code cells

- [ ] **Step 1: Perform the directory renames**

```bash
git mv gateway llm
git mv llm/llm_factory.py llm/factory.py
git mv tests/test_gateway tests/test_llm
git mv tests/test_llm/test_llm_factory.py tests/test_llm/test_factory.py
```

- [ ] **Step 2: Update all `from gateway.` imports**

```bash
# Most imports are from gateway.<module>; llm_factory specifically needs to become factory
grep -rln "from gateway\." --include="*.py" . | xargs sed -i '' 's/from gateway\./from llm./g'
# Fix the llm_factory → factory module rename within imports
grep -rln "from llm\.llm_factory" --include="*.py" . | xargs sed -i '' 's/from llm\.llm_factory/from llm.factory/g'
```

Verify:

```bash
grep -rn "from gateway\." --include="*.py" .
grep -rn "from llm\.llm_factory" --include="*.py" .
```

Both must return no matches.

- [ ] **Step 3: Update the four notebooks**

```bash
python3 <<'EOF'
import nbformat

notebooks = [
    "notebooks/test_team_construction.ipynb",
    "notebooks/test_report_agent.ipynb",
    "notebooks/test_compare_review.ipynb",
    "notebooks/test_data_query.ipynb",
]
for nb_path in notebooks:
    nb = nbformat.read(nb_path, as_version=4)
    changed = False
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        new_src = cell.source
        new_src = new_src.replace("from gateway.llm_factory", "from llm.factory")
        new_src = new_src.replace("from gateway.", "from llm.")
        if new_src != cell.source:
            cell.source = new_src
            changed = True
    if changed:
        nbformat.write(nb, nb_path)
        print(f"Updated {nb_path}")
    else:
        print(f"(no gateway imports in {nb_path})")
EOF
```

- [ ] **Step 4: Run the test suite**

```bash
pytest
```

Expected: all green. The `tests/test_llm/test_factory.py` file should discover and run.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: rename gateway/ to llm/ and llm_factory.py to factory.py"
```

---

## Task 3: Rename class `SimulatedDataGateway` → `LocalDataGateway`

**Files:**
- Modify: `datalayer/gateway.py` (rename class, add deprecated alias)
- Modify: every file matching `grep -rn "SimulatedDataGateway" --include="*.py" .`
- Modify: four notebook code cells (none of the four new notebooks reference the class by name — they only call the class methods via module imports — but Task-2 notebooks' `from datalayer.gateway import SimulatedDataGateway` will need updating if present; grep to confirm)

- [ ] **Step 1: Rename the class in `datalayer/gateway.py`**

Current (line 62):

```python
class SimulatedDataGateway(DataGateway):
    """In-memory gateway backed by per-case table data.
    ...
    """
```

Change to:

```python
class LocalDataGateway(DataGateway):
    """In-memory gateway backed by per-case table data.

    Data structure: {case_id: {table_name: [row_dicts]}}

    Loads from either the DataGenerator (synthetic cases) via
    :meth:`from_generated`, or from a folder of per-case CSV exports
    (real or synthetic-frozen) via :meth:`from_case_folders`.
    """
```

Then add at the bottom of the file (after `class LocalDataGateway` definition ends):

```python


# Backwards-compat alias — `SimulatedDataGateway` is the old name of the class
# that handles both simulated and real local CSV flavors. Kept for one cycle so
# external imports don't break; remove in a follow-up after internal call sites
# migrate (done here) and external consumers update.
SimulatedDataGateway = LocalDataGateway
```

- [ ] **Step 2: Update all call sites to use `LocalDataGateway`**

```bash
grep -rln "SimulatedDataGateway" --include="*.py" . | xargs sed -i '' 's/SimulatedDataGateway/LocalDataGateway/g'
```

Verify:

```bash
grep -rn "SimulatedDataGateway" --include="*.py" .
```

Expected output: only one line, the alias definition in `datalayer/gateway.py`.

- [ ] **Step 3: Update notebooks**

```bash
python3 <<'EOF'
import nbformat

notebooks = [
    "notebooks/test_team_construction.ipynb",
    "notebooks/test_report_agent.ipynb",
    "notebooks/test_compare_review.ipynb",
    "notebooks/test_data_query.ipynb",
]
for nb_path in notebooks:
    nb = nbformat.read(nb_path, as_version=4)
    changed = False
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        if "SimulatedDataGateway" in cell.source:
            cell.source = cell.source.replace("SimulatedDataGateway", "LocalDataGateway")
            changed = True
    if changed:
        nbformat.write(nb, nb_path)
        print(f"Updated {nb_path}")
    else:
        print(f"(no SimulatedDataGateway in {nb_path})")
EOF
```

- [ ] **Step 4: Run the test suite**

```bash
pytest
```

Expected: all green. Also run a quick import smoke-test to confirm the alias works:

```bash
python3 -c "from datalayer.gateway import SimulatedDataGateway, LocalDataGateway; assert SimulatedDataGateway is LocalDataGateway; print('alias ok')"
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: rename SimulatedDataGateway to LocalDataGateway (alias kept)"
```

---

## Task 4: Reshape `data_tables/` into `{simulated,real}/` subfolders

**Files:**
- Create: `data_tables/simulated/README.md`
- Create: `data_tables/real/README.md`
- Modify: `data_tables/README.md` (if present; update to describe the new layout)
- Verify: `.gitignore` already has `data_tables/*/` which ignores contents under any subfolder — no change needed.

- [ ] **Step 1: Create the two subdirectories with README files**

Check existing `data_tables/` content:

```bash
ls data_tables/
```

Create subdirs (if `data_tables/README.md` exists, preserve it):

```bash
mkdir -p data_tables/simulated data_tables/real
```

Write `data_tables/simulated/README.md`:

```markdown
# Simulated case CSVs

Drop per-case CSV exports from the generator (or hand-authored synthetic cases) under `data_tables/simulated/<case_id>/*.csv`.

The gateway loads this folder when `--data-source simulated` is passed to `main.py`, or when `--data-source auto` falls back here (real folder empty → this folder). Contents under case subfolders are gitignored.
```

Write `data_tables/real/README.md`:

```markdown
# Real case CSVs

Drop per-case exports from real systems under `data_tables/real/<case_id>/*.csv`.

The gateway loads this folder when `--data-source real` is passed to `main.py`, or when `--data-source auto` picks this folder (highest priority when non-empty). Contents under case subfolders are gitignored; never commit real customer data.
```

- [ ] **Step 2: Update top-level `data_tables/README.md` (if present)**

If the file exists, overwrite its content with a short pointer to the new structure:

```markdown
# data_tables/

Case-level CSV tables, split by source:

- `simulated/<case>/*.csv` — synthetic or hand-authored cases
- `real/<case>/*.csv` — real exports (never commit contents)

See `docs/specs/2026-04-25-data-layer-rename-and-pull-signal-design.md` for how the source is selected by `main.py`.
```

If the top-level `README.md` doesn't exist, create it with this content.

- [ ] **Step 3: Verify `.gitignore`**

```bash
grep "data_tables" .gitignore
```

Expected: `data_tables/*/` (or similar pattern covering subfolders) is present. If it's not, add:

```
data_tables/simulated/*/
data_tables/real/*/
```

- [ ] **Step 4: Commit**

```bash
git add data_tables/
# If .gitignore changed:
git add .gitignore
git commit -m "feat: split data_tables into simulated/ and real/ subfolders"
```

---

## Task 5: Data source selection in `main.py`

**Files:**
- Modify: `main.py` (argparse + `_resolve_data_source` helper + logger event)

- [ ] **Step 1: Add the `--data-source` CLI flag**

In `main.py`, within `amain()`, add the argparse argument (after the existing `--model` / `--seed` args):

```python
    parser.add_argument(
        "--data-source",
        choices=["auto", "real", "simulated", "generator"],
        default="auto",
        help="Where to load case data from. 'auto' resolves to real → simulated → generator.",
    )
```

- [ ] **Step 2: Replace the existing data-source block with a resolver**

Currently (lines ~74-87):

```python
    csv_gateway = SimulatedDataGateway.from_case_folders(str(_DATA_TABLES_DIR))
    if csv_gateway.list_case_ids():
        gateway = csv_gateway
        logger.log("data_source", {"source": "csv", "dir": str(_DATA_TABLES_DIR),
                                   "cases": csv_gateway.list_case_ids()})
    else:
        gen = DataGenerator(seed=args.seed, cases=50)
        gen.load_profiles()
        tables_raw = gen.generate_all()
        gateway = SimulatedDataGateway.from_generated(tables_raw)
        logger.log("data_source", {"source": "generator", "seed": args.seed})
```

Replace with a call to a new inline helper + selection logic. Add the helper at module level (after the `_REPORTS_DIR` / `_DATA_TABLES_DIR` constants):

```python
def _resolve_data_source(flag: str, tables_dir: Path) -> tuple[str, Path | None]:
    """Pick where case data comes from.

    Args:
        flag: one of "auto", "real", "simulated", "generator".
        tables_dir: root of the data_tables/ folder.

    Returns:
        (source_name, csv_dir) where csv_dir is None for the generator path.
        Raises SystemExit(2) if the user explicitly asked for real/simulated
        and that folder is empty.
    """
    real_dir = tables_dir / "real"
    sim_dir = tables_dir / "simulated"

    def _has_cases(p: Path) -> bool:
        return p.is_dir() and any(c.is_dir() for c in p.iterdir())

    if flag == "generator":
        return "generator", None
    if flag == "real":
        if not _has_cases(real_dir):
            raise SystemExit(f"--data-source real requested but {real_dir} is empty")
        return "real", real_dir
    if flag == "simulated":
        if not _has_cases(sim_dir):
            raise SystemExit(f"--data-source simulated requested but {sim_dir} is empty")
        return "simulated", sim_dir
    # auto
    if _has_cases(real_dir):
        return "real", real_dir
    if _has_cases(sim_dir):
        return "simulated", sim_dir
    return "generator", None
```

Then in `amain()`, the source block becomes:

```python
    source, csv_dir = _resolve_data_source(args.data_source, _DATA_TABLES_DIR)
    if source == "generator":
        gen = DataGenerator(seed=args.seed, cases=50)
        gen.load_profiles()
        tables_raw = gen.generate_all()
        gateway = LocalDataGateway.from_generated(tables_raw)
        logger.log("data_source", {"source": "generator", "path": None,
                                   "case_count": len(gateway.list_case_ids())})
    else:
        gateway = LocalDataGateway.from_case_folders(str(csv_dir))
        logger.log("data_source", {"source": source, "path": str(csv_dir),
                                   "case_count": len(gateway.list_case_ids())})
```

Note the class name is now `LocalDataGateway` (Task 3). If the import at the top of `main.py` still says `from datalayer.gateway import SimulatedDataGateway`, update it to `from datalayer.gateway import LocalDataGateway`.

- [ ] **Step 3: Write a unit test for the resolver**

Create `tests/test_main_resolver.py`:

```python
"""Unit tests for main._resolve_data_source."""
from pathlib import Path

import pytest

from main import _resolve_data_source


def _make_tables(tmp_path: Path, which: list[str]) -> Path:
    """Create a data_tables/ tree with the named subdirs each containing one case."""
    root = tmp_path / "data_tables"
    for name in which:
        case_dir = root / name / "CASE-00001"
        case_dir.mkdir(parents=True)
        (case_dir / "t.csv").write_text("a,b\n1,2\n")
    # Ensure the root exists even when no cases were requested
    root.mkdir(exist_ok=True)
    return root


def test_auto_prefers_real(tmp_path):
    root = _make_tables(tmp_path, ["real", "simulated"])
    assert _resolve_data_source("auto", root) == ("real", root / "real")


def test_auto_falls_back_to_simulated(tmp_path):
    root = _make_tables(tmp_path, ["simulated"])
    assert _resolve_data_source("auto", root) == ("simulated", root / "simulated")


def test_auto_falls_back_to_generator(tmp_path):
    root = _make_tables(tmp_path, [])
    assert _resolve_data_source("auto", root) == ("generator", None)


def test_real_flag_errors_when_empty(tmp_path):
    root = _make_tables(tmp_path, [])
    with pytest.raises(SystemExit):
        _resolve_data_source("real", root)


def test_simulated_flag_errors_when_empty(tmp_path):
    root = _make_tables(tmp_path, [])
    with pytest.raises(SystemExit):
        _resolve_data_source("simulated", root)


def test_generator_flag_always_works(tmp_path):
    root = _make_tables(tmp_path, ["real", "simulated"])
    assert _resolve_data_source("generator", root) == ("generator", None)
```

- [ ] **Step 4: Run the test and the full suite**

```bash
pytest tests/test_main_resolver.py -v
pytest
```

Expected: resolver tests pass; the full suite stays green (since the data-source selection logic only affects `main.py`'s runtime code path, not module imports).

- [ ] **Step 5: Smoke-run `main.py` with --help**

```bash
python3 main.py --help
```

Expected: the output shows `--data-source` in the argument list.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main_resolver.py
git commit -m "feat(main): --data-source flag with auto/real/simulated/generator"
```

---

## Task 6: Add `DataPullRequest` type and extend `FinalAnswer`

**Files:**
- Modify: `models/types.py`

- [ ] **Step 1: Add `DataPullRequest` type**

At the bottom of `models/types.py`, after `FinalAnswer`, add:

```python
class DataPullRequest(BaseModel):
    """Advisory signal emitted by the Balance step when specialist `data_gaps`
    and report `coverage` together suggest the answer is materially incomplete.

    No live pull backend exists today — this documents what a future Data Agent
    would target. Rendered to the reviewer by `ChatAgent.format_final_answer`.
    """
    needed: bool
    reason: str
    would_pull: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"]
```

- [ ] **Step 2: Place `DataPullRequest` before `FinalAnswer`, extend `FinalAnswer`**

Move the `DataPullRequest` class definition to appear **before** `FinalAnswer` in `models/types.py` (not after). This avoids needing a forward-reference string annotation.

Then in `FinalAnswer` (around lines 174–194 in the current file), add as the last field:

```python
    data_pull_request: DataPullRequest | None = None
```

- [ ] **Step 3: Write a unit test for the new type**

Create or extend `tests/test_models/test_data_pull_request.py`:

```python
"""Unit tests for DataPullRequest and FinalAnswer.data_pull_request."""
from models.types import (
    DataPullRequest,
    FinalAnswer,
    ReportDraft,
    TeamDraft,
)


def _minimal_drafts():
    return (
        ReportDraft(coverage="none"),
        TeamDraft(answer="test"),
    )


def test_data_pull_request_basic():
    dpr = DataPullRequest(
        needed=True,
        reason="Missing bureau refresh",
        would_pull=["bureau.fico_latest"],
        severity="medium",
    )
    assert dpr.needed is True
    assert dpr.would_pull == ["bureau.fico_latest"]


def test_final_answer_default_has_no_pull_request():
    report, team = _minimal_drafts()
    fa = FinalAnswer(answer="ok", report_draft=report, team_draft=team)
    assert fa.data_pull_request is None


def test_final_answer_with_pull_request():
    report, team = _minimal_drafts()
    dpr = DataPullRequest(
        needed=True, reason="x", would_pull=[], severity="low",
    )
    fa = FinalAnswer(
        answer="ok", report_draft=report, team_draft=team,
        data_pull_request=dpr,
    )
    assert fa.data_pull_request is not None
    assert fa.data_pull_request.severity == "low"
```

- [ ] **Step 4: Run the test**

```bash
pytest tests/test_models/test_data_pull_request.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Run the full suite**

```bash
pytest
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add models/types.py tests/test_models/test_data_pull_request.py
git commit -m "feat(models): add DataPullRequest + FinalAnswer.data_pull_request"
```

---

## Task 7: Extend the Balancing skill to emit `data_pull_request`

**Files:**
- Modify: `skills/workflow/balancing.md`

- [ ] **Step 1: Update the frontmatter `outputs` section**

Current frontmatter lists:

```yaml
outputs:
  answer: str
  flags: list
```

Change to:

```yaml
outputs:
  answer: str
  flags: list
  data_pull_request: object | null
```

- [ ] **Step 2: Add a new section below "Flag conventions" and above "Output format"**

Insert this section verbatim:

```markdown
# Data pull request

Beyond merging answers, judge whether the combined evidence is enough to answer the reviewer's question with confidence. Look at:

- Specialist `data_gaps` (noted in the team draft)
- Report `coverage` (`full`, `partial`, or `none`)
- Unresolved `open_conflicts` driven by missing evidence rather than genuine disagreement

If these together indicate the answer is materially incomplete — e.g., multiple specialists flagged missing data, coverage is `partial` or `none`, or an open conflict cannot be resolved without more data — emit a `data_pull_request` in the output JSON:

- `needed: true` when the signal is clear; `false` otherwise (or omit the field).
- `reason`: one sentence describing why the current data is insufficient.
- `would_pull`: free-text list of the kinds of data that would help (e.g., `"bureau refresh from last 90 days"`, `"returned payment reasons for 2025-Q4"`). Match the phrasing of existing `data_gaps` where possible.
- `severity`: `"low"` (nice-to-have), `"medium"` (would materially tighten the answer), or `"high"` (answer is unreliable without it).

If the combined drafts cleanly answer the question, omit `data_pull_request` or set `needed: false`.
```

- [ ] **Step 3: Update the "Output format" section**

Currently:

```markdown
# Output format

Return JSON:

\`\`\`json
{
  "answer": "merged reviewer-facing answer, 1-3 paragraphs",
  "flags": ["one-line note per discrepancy or caveat"]
}
\`\`\`
```

Extend to include the new optional field:

```markdown
# Output format

Return JSON:

\`\`\`json
{
  "answer": "merged reviewer-facing answer, 1-3 paragraphs",
  "flags": ["one-line note per discrepancy or caveat"],
  "data_pull_request": {
    "needed": true,
    "reason": "one-sentence reason",
    "would_pull": ["free-text phrase", "..."],
    "severity": "low | medium | high"
  }
}
\`\`\`

`data_pull_request` is optional — omit it entirely when no pull is warranted.
```

(Note: in the actual file, the triple-backticks above shouldn't be escaped. Remove the backslashes before each backtick when editing.)

- [ ] **Step 4: Commit**

```bash
git add skills/workflow/balancing.md
git commit -m "feat(skills): balancing emits optional data_pull_request"
```

No unit tests here — the skill body is prose consumed by the LLM; behavior is validated by integration tests in Task 8.

---

## Task 8: Wire `DataPullRequest` parsing in `Orchestrator.balance`

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Modify: `tests/test_orchestrator/test_orchestrator.py` (add parsing tests)

- [ ] **Step 1: Update `Orchestrator.balance` to read the new field**

At the top of `orchestrator/orchestrator.py`, add to the `from models.types import` block:

```python
    DataPullRequest,
```

In the `balance` method, currently:

```python
        data = result.data
        answer = str(data.get("answer", "")).strip()
        flags = data.get("flags", []) or []
        if not isinstance(flags, list):
            flags = []

        if not answer:
            return self._balance_fallback(report_draft, team_draft)

        return FinalAnswer(
            answer=answer,
            flags=[str(f) for f in flags],
            report_draft=report_draft,
            team_draft=team_draft,
        )
```

Add `DataPullRequest` parsing before the `return`:

```python
        data = result.data
        answer = str(data.get("answer", "")).strip()
        flags = data.get("flags", []) or []
        if not isinstance(flags, list):
            flags = []

        if not answer:
            return self._balance_fallback(report_draft, team_draft)

        dpr = self._parse_data_pull_request(data.get("data_pull_request"))
        if dpr is not None and dpr.needed:
            flags = ["data insufficient — pull recommended"] + [str(f) for f in flags]
            self.logger.log("data_pull_requested", {
                "would_pull": dpr.would_pull,
                "severity": dpr.severity,
                "reason": dpr.reason,
            })
        else:
            flags = [str(f) for f in flags]

        return FinalAnswer(
            answer=answer,
            flags=flags,
            report_draft=report_draft,
            team_draft=team_draft,
            data_pull_request=dpr,
        )
```

Then add the parser helper as a static method on `Orchestrator`:

```python
    @staticmethod
    def _parse_data_pull_request(raw) -> DataPullRequest | None:
        """Construct a DataPullRequest from the raw LLM dict, or None on any
        parse failure. Returning None is fine — the field is optional.
        """
        if not isinstance(raw, dict):
            return None
        try:
            return DataPullRequest(
                needed=bool(raw.get("needed", False)),
                reason=str(raw.get("reason", "")),
                would_pull=[str(x) for x in (raw.get("would_pull") or [])
                            if isinstance(x, (str, int, float))],
                severity=raw.get("severity") if raw.get("severity") in ("low", "medium", "high") else "low",
            )
        except Exception:
            return None
```

- [ ] **Step 2: Verify the fallback path still returns None**

The `_balance_fallback` static method already returns a `FinalAnswer` without `data_pull_request` set, which defaults to `None`. Confirm by reading the method — no code change needed unless a test fails.

- [ ] **Step 3: Add unit tests for the parser and the balance flow**

Append to `tests/test_orchestrator/test_orchestrator.py` (or create a new test file):

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from models.types import DataPullRequest, ReportDraft, TeamDraft, LLMResult
from orchestrator.orchestrator import Orchestrator


def test_parse_data_pull_request_valid():
    raw = {
        "needed": True,
        "reason": "Missing bureau refresh",
        "would_pull": ["bureau.fico_latest_90d"],
        "severity": "medium",
    }
    dpr = Orchestrator._parse_data_pull_request(raw)
    assert dpr is not None
    assert dpr.needed is True
    assert dpr.severity == "medium"


def test_parse_data_pull_request_none_on_non_dict():
    assert Orchestrator._parse_data_pull_request(None) is None
    assert Orchestrator._parse_data_pull_request("x") is None


def test_parse_data_pull_request_coerces_bad_severity():
    raw = {"needed": True, "reason": "x", "would_pull": [], "severity": "bogus"}
    dpr = Orchestrator._parse_data_pull_request(raw)
    assert dpr is not None
    assert dpr.severity == "low"  # coerced to default


def test_parse_data_pull_request_filters_non_string_items():
    raw = {
        "needed": True, "reason": "x",
        "would_pull": ["good", {"bad": True}, 42, None, "also good"],
        "severity": "high",
    }
    dpr = Orchestrator._parse_data_pull_request(raw)
    assert dpr.would_pull == ["good", "42", "also good"]  # 42 coerces to "42"


@pytest.mark.asyncio
async def test_balance_attaches_pull_request_and_prepends_flag():
    """When the balance LLM returns a data_pull_request with needed=True,
    balance() should attach it to FinalAnswer and prepend an insufficient-data flag."""
    logger = MagicMock()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={
            "answer": "merged answer",
            "flags": ["existing flag"],
            "data_pull_request": {
                "needed": True,
                "reason": "missing stuff",
                "would_pull": ["stuff"],
                "severity": "high",
            },
        },
    ))
    orchestrator = Orchestrator(
        mock_llm, logger, MagicMock(), "credit_risk", pillar_config={}, catalog=None,
    )

    report = ReportDraft(coverage="none")
    team = TeamDraft(answer="team draft")
    final = await orchestrator.balance("q?", report, team)

    assert final.data_pull_request is not None
    assert final.data_pull_request.needed is True
    assert final.flags[0] == "data insufficient — pull recommended"
    logger.log.assert_any_call("data_pull_requested", {
        "would_pull": ["stuff"], "severity": "high", "reason": "missing stuff",
    })


@pytest.mark.asyncio
async def test_balance_no_pull_request_when_not_needed():
    logger = MagicMock()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"answer": "merged answer", "flags": []},
    ))
    orchestrator = Orchestrator(
        mock_llm, logger, MagicMock(), "credit_risk", pillar_config={}, catalog=None,
    )

    report = ReportDraft(coverage="full", answer="r")
    team = TeamDraft(answer="t")
    final = await orchestrator.balance("q?", report, team)

    assert final.data_pull_request is None
    assert "data insufficient — pull recommended" not in final.flags
```

Make sure `pytest-asyncio` is available (check the existing tests — if they already use `@pytest.mark.asyncio`, it's configured). If not, the existing project uses `pytest.mark.asyncio` elsewhere — confirm by checking one of the existing async tests.

- [ ] **Step 4: Run the tests**

```bash
pytest tests/test_orchestrator/ -v
pytest
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/test_orchestrator/
git commit -m "feat(orchestrator): parse data_pull_request in balance, emit logger event"
```

---

## Task 9: Render `DataPullRequest` in `ChatAgent.format_final_answer`

**Files:**
- Modify: `orchestrator/chat_agent.py`
- Modify or create: `tests/test_orchestrator/test_chat_agent.py`

- [ ] **Step 1: Update `format_final_answer`**

Currently the method produces sections: Answer, Flags, Provenance, Timeline. Add a new section for the pull recommendation, inserted before the Timeline section and after Provenance:

```python
    @staticmethod
    def format_final_answer(final: FinalAnswer) -> str:
        """Render a FinalAnswer as reviewer-facing markdown.

        Sections: Answer, Flags (if any), Provenance, Data pull recommendation
        (if any), Timeline (per-stage duration).
        """
        parts: list[str] = ["## Answer\n", final.answer]
        if final.flags:
            parts.append("\n## Flags")
            for flag in final.flags:
                parts.append(f"- {flag}")
        parts.append(
            "\n## Provenance\n"
            f"- Report coverage: {final.report_draft.coverage}\n"
            f"- Files consulted: {final.report_draft.files_consulted or '(none)'}\n"
            f"- Specialists consulted: {final.team_draft.specialists_consulted or '(none)'}"
        )

        dpr = final.data_pull_request
        if dpr is not None and dpr.needed:
            would_pull_str = ", ".join(dpr.would_pull) if dpr.would_pull else "(nothing specific flagged)"
            parts.append(
                f"\n## Data pull recommendation (severity: {dpr.severity})\n"
                f"Reason: {dpr.reason}\n\n"
                f"Would pull: {would_pull_str}\n\n"
                f"> No live pull today — the Data Agent is not deployed yet. "
                f"This is a signal of what a future pull would target."
            )

        if final.timeline:
            parts.append("\n## Timeline")
            for entry in final.timeline:
                parts.append(
                    f"- **{entry['stage']}**: {entry['duration_ms']} ms"
                )
        return "\n".join(parts)
```

- [ ] **Step 2: Add / extend unit tests**

Create `tests/test_orchestrator/test_chat_agent.py` (or extend if it exists):

```python
"""Tests for ChatAgent.format_final_answer — specifically the data-pull section."""
from models.types import (
    DataPullRequest,
    FinalAnswer,
    ReportDraft,
    TeamDraft,
)
from orchestrator.chat_agent import ChatAgent


def _final(data_pull_request=None, flags=None):
    return FinalAnswer(
        answer="test answer",
        flags=flags or [],
        report_draft=ReportDraft(coverage="partial"),
        team_draft=TeamDraft(answer="team answer", specialists_consulted=["bureau"]),
        data_pull_request=data_pull_request,
    )


def test_format_without_pull_request_omits_section():
    final = _final()
    formatted = ChatAgent.format_final_answer(final)
    assert "Data pull recommendation" not in formatted


def test_format_with_pull_request_renders_section():
    dpr = DataPullRequest(
        needed=True,
        reason="Need bureau refresh",
        would_pull=["bureau.fico_latest", "spend_payments.returned_reasons"],
        severity="high",
    )
    final = _final(data_pull_request=dpr)
    formatted = ChatAgent.format_final_answer(final)
    assert "Data pull recommendation (severity: high)" in formatted
    assert "Need bureau refresh" in formatted
    assert "bureau.fico_latest" in formatted
    assert "spend_payments.returned_reasons" in formatted
    assert "No live pull today" in formatted


def test_format_with_needed_false_omits_section():
    dpr = DataPullRequest(
        needed=False, reason="ok", would_pull=[], severity="low",
    )
    final = _final(data_pull_request=dpr)
    formatted = ChatAgent.format_final_answer(final)
    assert "Data pull recommendation" not in formatted


def test_format_with_empty_would_pull_shows_placeholder():
    dpr = DataPullRequest(
        needed=True, reason="generic concern", would_pull=[], severity="low",
    )
    final = _final(data_pull_request=dpr)
    formatted = ChatAgent.format_final_answer(final)
    assert "Would pull: (nothing specific flagged)" in formatted
```

- [ ] **Step 3: Run the tests**

```bash
pytest tests/test_orchestrator/test_chat_agent.py -v
pytest
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/chat_agent.py tests/test_orchestrator/test_chat_agent.py
git commit -m "feat(chat): render DataPullRequest in format_final_answer"
```

---

## Self-review (implementer sanity check)

Before marking the plan complete:

1. **`grep -rn "from data\." --include="*.py" .`** returns no matches.
2. **`grep -rn "from gateway\." --include="*.py" .`** returns no matches.
3. **`grep -rn "SimulatedDataGateway" --include="*.py" .`** returns exactly one line — the alias in `datalayer/gateway.py`.
4. **`python3 -c "from datalayer.gateway import SimulatedDataGateway, LocalDataGateway; assert SimulatedDataGateway is LocalDataGateway"`** succeeds.
5. **`pytest`** is all green.
6. **`python3 main.py --help`** shows the `--data-source` flag.
7. **`data_tables/simulated/` and `data_tables/real/` exist** with READMEs.
8. **`skills/workflow/balancing.md`** has the new "Data pull request" section and updated output format.
9. **`models/types.py`** has `DataPullRequest` and `FinalAnswer.data_pull_request`.
10. **9 commits on `main`**, one per task, each with a scope matching its task.

If any of the above fails, fix and commit separately.
