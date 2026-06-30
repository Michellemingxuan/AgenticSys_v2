# DataAgent Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the offline profile-reconciliation / catalog-curation subsystem into a sibling `../DataAgent` code-only plug-in, leaving AgenticSys_v2 a pure consumer of the data it already holds.

**Architecture:** DataAgent is a new top-level package `dataagent/` in `../DataAgent`. It imports AgenticSys_v2's staying modules (`datalayer.catalog/gateway/adapter`, `llm`, `logger`, `models`) from the sibling checkout (added to `sys.path` by `dataagent/__init__.py`), and reads/writes AgenticSys_v2's data folders (`config/data_profiles/`, `context/`, `data_tables/`, `.catalog_verified/`) via a `dataagent/paths.py` resolver. The dependency is strictly one-way; AgenticSys_v2 ends with zero references to the moved code.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, Flask (catalog viewer host), pandas (reconciliation). No new third-party deps.

## Global Constraints

- **DataAgent is code-only.** No data files in `../DataAgent`. All data (`config/data_profiles/*.yaml`, `.provenance.json`, `context/*.txt`, `data_tables/`, `.catalog_verified/`) stays committed in AgenticSys_v2.
- **One-way dependency.** DataAgent → AgenticSys_v2 only. AgenticSys_v2 must never import `dataagent.*`.
- **New package name `dataagent/`.** Moved peer modules are referenced as `dataagent.*`; references to staying modules keep `datalayer.*`/`llm.*`/`logger.*`/`models.*` (resolved from the sibling).
- **Repoint every `__file__`-relative or repo-relative data path** in moved modules through `dataagent.paths` — once under `dataagent/`, `Path(__file__)` resolves to the wrong repo.
- **`adapter.py` and `sync_catalog()` stay** in AgenticSys_v2 untouched (runtime per-case sync; non-test caller in `notebooks/run_question_suite.py`).
- **Two git repos.** Tasks 1–6 commit in `../DataAgent` (run `git init` in Task 1). Task 7 commits in AgenticSys_v2. Do NOT push. Commit only with the user's go-ahead.
- **Sibling layout assumption:** `../DataAgent` and `../AgenticSys_v2` are checked out side by side. `AGENTICSYS_ROOT` env var overrides the default sibling path.
- **Absolute paths in commands:** AgenticSys_v2 root is `/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2`; DataAgent root is the sibling `.../Projs/DataAgent`.

---

### Task 1: Scaffold the DataAgent package + path resolver

**Files:**
- Create: `../DataAgent/dataagent/__init__.py`
- Create: `../DataAgent/dataagent/paths.py`
- Create: `../DataAgent/pyproject.toml`
- Create: `../DataAgent/conftest.py`
- Create: `../DataAgent/README.md`
- Test: `../DataAgent/tests/test_paths.py`

**Interfaces:**
- Produces: `dataagent.paths.agenticsys_root() -> Path`, `profiles_dir()`, `provenance_path()`, `context_dir()`, `data_tables_dir()`, `snapshot_root()` — all `-> Path`. `dataagent/__init__.py` inserts `agenticsys_root()` onto `sys.path` at import time so `import datalayer.catalog` resolves from the sibling.

- [ ] **Step 1: Initialize the repo and package dirs**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
git init
mkdir -p dataagent tests
```

- [ ] **Step 2: Write `dataagent/paths.py`**

```python
"""Resolve the AgenticSys_v2 data folders DataAgent reads/writes.

DataAgent holds no data of its own. These resolvers point at the sibling
AgenticSys_v2 checkout (override with the AGENTICSYS_ROOT env var).
"""
from __future__ import annotations

import os
from pathlib import Path

# ../DataAgent/dataagent/paths.py -> parents[2] == .../Projs
_DEFAULT_SIBLING = Path(__file__).resolve().parents[2] / "AgenticSys_v2"


def agenticsys_root() -> Path:
    return Path(os.environ.get("AGENTICSYS_ROOT", _DEFAULT_SIBLING)).resolve()


def profiles_dir() -> Path:
    return agenticsys_root() / "config" / "data_profiles"


def provenance_path() -> Path:
    return profiles_dir() / ".provenance.json"


def context_dir() -> Path:
    return agenticsys_root() / "context"


def data_tables_dir() -> Path:
    return agenticsys_root() / "data_tables"


def snapshot_root() -> Path:
    return agenticsys_root() / ".catalog_verified"
```

- [ ] **Step 3: Write `dataagent/__init__.py` (sibling on sys.path)**

```python
"""DataAgent — offline profile-reconciliation / catalog-curation plug-in.

Imports AgenticSys_v2's staying modules (datalayer.catalog/gateway/adapter,
llm, logger, models) from the sibling checkout. The sibling root is added to
sys.path here so both `python -m dataagent.*` CLIs and the test suite resolve
those imports without an editable install.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SIBLING = Path(
    os.environ.get(
        "AGENTICSYS_ROOT",
        Path(__file__).resolve().parents[2] / "AgenticSys_v2",
    )
).resolve()

if str(_SIBLING) not in sys.path:
    sys.path.insert(0, str(_SIBLING))
```

- [ ] **Step 4: Write `conftest.py`, `pyproject.toml`, `README.md`**

`../DataAgent/conftest.py`:
```python
"""Ensure the dataagent package (and, via its __init__, the AgenticSys_v2
sibling) is importable when running the test suite from the repo root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dataagent  # noqa: F401,E402  (runs the sys.path bootstrap)
```

`../DataAgent/pyproject.toml`:
```toml
[project]
name = "dataagent"
version = "0.1.0"
description = "Offline profile-reconciliation / catalog-curation plug-in for AgenticSys_v2"
requires-python = ">=3.11"

[tool.setuptools.packages.find]
include = ["dataagent*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

`../DataAgent/README.md`:
```markdown
# DataAgent

Offline profile-reconciliation / catalog-curation plug-in for **AgenticSys_v2**.

DataAgent holds only code. All data (data profiles, provenance, context files,
data tables) lives in the sibling **AgenticSys_v2** checkout, which must sit
beside this repo:

    Projs/
      AgenticSys_v2/
      DataAgent/

`dataagent/__init__.py` adds AgenticSys_v2 to `sys.path` automatically. To point
at a non-sibling checkout, set `AGENTICSYS_ROOT=/path/to/AgenticSys_v2`.

## CLIs
    python -m dataagent.sync              # reconcile real-data CSVs against the catalog
    python -m dataagent.verify_snapshot   # snapshot / list / diff / restore verified state
    python -m dataagent.viewer            # serve the /catalog curation UI
```

- [ ] **Step 5: Write the failing test `../DataAgent/tests/test_paths.py`**

```python
import os
from pathlib import Path

from dataagent import paths


def test_default_root_is_sibling_agenticsys():
    root = paths.agenticsys_root()
    assert root.name == "AgenticSys_v2"
    assert paths.profiles_dir() == root / "config" / "data_profiles"
    assert paths.provenance_path() == root / "config" / "data_profiles" / ".provenance.json"
    assert paths.context_dir() == root / "context"
    assert paths.snapshot_root() == root / ".catalog_verified"


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTICSYS_ROOT", str(tmp_path))
    assert paths.agenticsys_root() == tmp_path.resolve()
    assert paths.data_tables_dir() == tmp_path.resolve() / "data_tables"
```

- [ ] **Step 6: Run tests and verify the sibling import seam**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
python -m pytest tests/test_paths.py -v
python -c "import dataagent; import datalayer.catalog; print('seam OK:', datalayer.catalog.__file__)"
```
Expected: 2 passed; the `seam OK:` line prints a path inside `AgenticSys_v2/datalayer/catalog.py`.

- [ ] **Step 7: Commit (DataAgent repo, with user go-ahead)**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
printf "__pycache__/\n*.pyc\n.pytest_cache/\n" > .gitignore
git add -A
git commit -m "feat: scaffold dataagent plug-in package + path resolver"
```

---

### Task 2: Move the leaf modules — `context_dict.py` + `provenance.py`

These have NO peer imports (pure stdlib), so they move with zero import rewrites. They are the dependencies of everything else.

**Files:**
- Create: `../DataAgent/dataagent/context_dict.py` (copy of `datalayer/context_dict.py`)
- Create: `../DataAgent/dataagent/provenance.py` (copy of `datalayer/provenance.py`)
- Create: `../DataAgent/tests/test_context_dict.py` (copy of `tests/test_datalayer/test_context_dict.py`)
- Create: `../DataAgent/tests/test_provenance.py` (copy of `tests/test_datalayer/test_provenance.py`)

**Interfaces:**
- Produces: `dataagent.context_dict` (exports `ContextEntry`, `load_context_by_table`, `normalize_threshold`, `update_context_entry`, `normalize_key`) and `dataagent.provenance` (exports `Provenance`) — same public API as the originals.

- [ ] **Step 1: Copy the modules and their tests**

```bash
SRC="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
DST="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
cp "$SRC/datalayer/context_dict.py" "$DST/dataagent/context_dict.py"
cp "$SRC/datalayer/provenance.py"   "$DST/dataagent/provenance.py"
cp "$SRC/tests/test_datalayer/test_context_dict.py" "$DST/tests/test_context_dict.py"
cp "$SRC/tests/test_datalayer/test_provenance.py"   "$DST/tests/test_provenance.py"
```

- [ ] **Step 2: Rewrite the test imports `datalayer.* → dataagent.*`**

In `../DataAgent/tests/test_context_dict.py` and `../DataAgent/tests/test_provenance.py`, replace every:
- `from datalayer.context_dict import` → `from dataagent.context_dict import`
- `import datalayer.context_dict` → `import dataagent.context_dict`
- `from datalayer.provenance import` → `from dataagent.provenance import`
- `import datalayer.provenance` → `import dataagent.provenance`

(Find them with: `grep -n "datalayer" "$DST/tests/test_context_dict.py" "$DST/tests/test_provenance.py"`. The module bodies themselves need NO edits — verify with `grep -n "datalayer" "$DST/dataagent/context_dict.py" "$DST/dataagent/provenance.py"` returning nothing.)

- [ ] **Step 3: Run tests**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
python -m pytest tests/test_context_dict.py tests/test_provenance.py -v
```
Expected: PASS (same counts as the originals in AgenticSys_v2).

- [ ] **Step 4: Commit (DataAgent, with user go-ahead)**

```bash
git add -A && git commit -m "feat: move context_dict + provenance into dataagent"
```

---

### Task 3: Move `reconcile.py`

Depends on `context_dict` (peer → `dataagent`) and on staying modules (`catalog`/`gateway`/`adapter`, resolved from sibling).

**Files:**
- Create: `../DataAgent/dataagent/reconcile.py` (copy of `datalayer/reconcile.py`)
- Create: `../DataAgent/tests/test_reconcile.py` (copy of `tests/test_datalayer/test_reconcile.py`)

**Interfaces:**
- Consumes: `dataagent.context_dict` (Task 2); sibling `datalayer.catalog`, `datalayer.gateway`, `datalayer.adapter`.
- Produces: `dataagent.reconcile` (exports `reconcile`, `check_consistency`, `ReconcileResult`) — same API.

- [ ] **Step 1: Copy module + test**

```bash
SRC="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
DST="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
cp "$SRC/datalayer/reconcile.py" "$DST/dataagent/reconcile.py"
cp "$SRC/tests/test_datalayer/test_reconcile.py" "$DST/tests/test_reconcile.py"
```

- [ ] **Step 2: Rewrite the one peer import in `dataagent/reconcile.py`**

Edit `../DataAgent/dataagent/reconcile.py` line ~11:
- old: `from datalayer.context_dict import update_context_entry, normalize_key`
- new: `from dataagent.context_dict import update_context_entry, normalize_key`

Leave all `datalayer.catalog` / `datalayer.gateway` / `datalayer.adapter` imports unchanged (they resolve from the sibling). Confirm: `grep -n "datalayer" "$DST/dataagent/reconcile.py"` shows only catalog/gateway/adapter lines.

- [ ] **Step 3: Rewrite test imports in `tests/test_reconcile.py`**

Replace `from datalayer.reconcile import` → `from dataagent.reconcile import` and `from datalayer.context_dict import` → `from dataagent.context_dict import`. Leave `datalayer.catalog`/`datalayer.gateway`/`datalayer.adapter` test imports as-is. (`grep -n "datalayer" tests/test_reconcile.py` to find them.)

- [ ] **Step 4: Run tests**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
python -m pytest tests/test_reconcile.py -v
```
Expected: PASS (same count as the original).

- [ ] **Step 5: Commit (DataAgent, with user go-ahead)**

```bash
git add -A && git commit -m "feat: move reconcile into dataagent"
```

---

### Task 4: Move `sync.py` (CLI) + repoint data paths

Depends on `reconcile`, `context_dict`, `provenance` (peers → `dataagent`) and sibling `adapter`/`catalog`/`gateway`. Has the most hardcoded paths.

**Files:**
- Create: `../DataAgent/dataagent/sync.py` (copy of `datalayer/sync.py`)
- Create: `../DataAgent/tests/test_sync.py`, `tests/test_reconcile_cli.py`, `tests/test_sync_interactive_demo.py` (copies)

**Interfaces:**
- Consumes: `dataagent.reconcile/context_dict/provenance`, `dataagent.paths`; sibling `datalayer.adapter/catalog/gateway`.
- Produces: `dataagent.sync` CLI (`python -m dataagent.sync`) with `run_reconcile(...)`.

- [ ] **Step 1: Copy module + tests**

```bash
SRC="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
DST="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
cp "$SRC/datalayer/sync.py" "$DST/dataagent/sync.py"
cp "$SRC/tests/test_sync.py" "$DST/tests/test_sync.py"
cp "$SRC/tests/test_datalayer/test_reconcile_cli.py" "$DST/tests/test_reconcile_cli.py"
cp "$SRC/tests/test_sync_interactive_demo.py" "$DST/tests/test_sync_interactive_demo.py"
```

- [ ] **Step 2: Rewrite peer imports in `dataagent/sync.py`**

Edit `../DataAgent/dataagent/sync.py`:
- line 31: `from datalayer.context_dict import load_context_by_table` → `from dataagent.context_dict import load_context_by_table`
- line 33: `from datalayer.provenance import Provenance` → `from dataagent.provenance import Provenance`
- line 34: `from datalayer.reconcile import reconcile, ReconcileResult` → `from dataagent.reconcile import reconcile, ReconcileResult`

Leave line 29 (`from datalayer import adapter`), line 30 (`from datalayer.catalog import DataCatalog`), line 32 (`from datalayer.gateway import LocalDataGateway`) unchanged.

- [ ] **Step 3: Repoint the hardcoded data paths through `dataagent.paths`**

Add `from dataagent import paths` near the other imports, then change:
- line 77 `_REPO_ROOT = Path(__file__).parent.parent` → `_REPO_ROOT = paths.agenticsys_root()`
- line 78 `_REAL_DIR = _REPO_ROOT / "data_tables" / "real"` → `_REAL_DIR = paths.data_tables_dir() / "real"`
- line 79 `_SIM_DIR = _REPO_ROOT / "data_tables" / "simulated"` → `_SIM_DIR = paths.data_tables_dir() / "simulated"`
- line 579 `profile_dir: str = "config/data_profiles",` → `profile_dir: str = str(paths.profiles_dir()),`
- line 647 `parser.add_argument("--data-dir", default="data_tables/real",` → `parser.add_argument("--data-dir", default=str(paths.data_tables_dir() / "real"),`

Line 618 (`Provenance(os.path.join(profile_dir, ".provenance.json"))`) needs no change — `profile_dir` now defaults to the sibling. Update the module docstring usage lines (4–11) `python -m datalayer.sync` → `python -m dataagent.sync`.

- [ ] **Step 4: Rewrite test imports**

In the three copied tests, replace `from datalayer.sync import`/`import datalayer.sync` → `dataagent.sync`, and any `datalayer.reconcile`/`context_dict`/`provenance` peer imports → `dataagent.*`. Leave `datalayer.catalog`/`gateway`/`adapter` as-is. These tests already isolate data via tmp dirs/fixtures — confirm none write to the real sibling `config/data_profiles` (grep for `paths.profiles_dir`/`config/data_profiles` and ensure any such use is monkeypatched or tmp).

- [ ] **Step 5: Run tests**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
python -m pytest tests/test_sync.py tests/test_reconcile_cli.py tests/test_sync_interactive_demo.py -v
```
Expected: PASS (same counts as originals).

- [ ] **Step 6: Commit (DataAgent, with user go-ahead)**

```bash
git add -A && git commit -m "feat: move sync CLI into dataagent; repoint data paths via dataagent.paths"
```

---

### Task 5: Move `verify_snapshot.py` (CLI) + repoint snapshot paths

Pure stdlib; only hardcoded path constants to repoint.

**Files:**
- Create: `../DataAgent/dataagent/verify_snapshot.py` (copy)
- Create: `../DataAgent/tests/test_verify_snapshot.py` (copy)

**Interfaces:**
- Consumes: `dataagent.paths`.
- Produces: `dataagent.verify_snapshot` CLI (`python -m dataagent.verify_snapshot snapshot|list|diff|restore`).

- [ ] **Step 1: Copy module + test**

```bash
SRC="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
DST="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
cp "$SRC/datalayer/verify_snapshot.py" "$DST/dataagent/verify_snapshot.py"
cp "$SRC/tests/test_datalayer/test_verify_snapshot.py" "$DST/tests/test_verify_snapshot.py"
```

- [ ] **Step 2: Repoint the default path constants**

Add `from dataagent import paths` to `../DataAgent/dataagent/verify_snapshot.py`, then change lines 43–46:
- `_DEFAULT_CONTEXT_DIR = "context"` → `_DEFAULT_CONTEXT_DIR = str(paths.context_dir())`
- `_DEFAULT_PROFILE_DIR = "config/data_profiles"` → `_DEFAULT_PROFILE_DIR = str(paths.profiles_dir())`
- `_DEFAULT_PROVENANCE = "config/data_profiles/.provenance.json"` → `_DEFAULT_PROVENANCE = str(paths.provenance_path())`
- `_DEFAULT_SNAPSHOT_ROOT = ".catalog_verified"` → `_DEFAULT_SNAPSHOT_ROOT = str(paths.snapshot_root())`

Update any `python -m datalayer.verify_snapshot` strings in the docstring → `python -m dataagent.verify_snapshot`.

- [ ] **Step 3: Rewrite test imports + isolation check**

Replace `datalayer.verify_snapshot` → `dataagent.verify_snapshot` in `tests/test_verify_snapshot.py`. These tests use tmp snapshot roots — confirm they pass explicit dirs (not the repointed defaults) so they never touch the real sibling `.catalog_verified`. Where a test relied on the old repo-relative default, point it at `tmp_path` explicitly.

- [ ] **Step 4: Run tests**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
python -m pytest tests/test_verify_snapshot.py -v
```
Expected: PASS (22 tests, per the original suite).

- [ ] **Step 5: Commit (DataAgent, with user go-ahead)**

```bash
git add -A && git commit -m "feat: move verify_snapshot into dataagent; repoint snapshot paths"
```

---

### Task 6: Move the catalog viewer + standalone host

`catalog_view.py` (builds the view) + `catalog_page.py` (routes/reconcile trigger) + a new `dataagent/viewer.py` Flask host.

**Files:**
- Create: `../DataAgent/dataagent/catalog_view.py` (copy of `tools/node_trace/catalog_view.py`)
- Create: `../DataAgent/dataagent/catalog_page.py` (copy of `tools/node_trace/catalog_page.py`)
- Create: `../DataAgent/dataagent/viewer.py` (new host)
- Create: `../DataAgent/tests/test_catalog_view.py` (copy of `tests/test_node_trace/test_catalog_view.py`)

**Interfaces:**
- Consumes: `dataagent.provenance`, `dataagent.context_dict`, `dataagent.paths`; sibling `datalayer.catalog`, `datalayer.gateway`. Reconcile trigger shells `python -m dataagent.sync`.
- Produces: `dataagent.catalog_view.build_catalog_view(...)`, `dataagent.catalog_page.register_catalog_routes(app)`, `dataagent.viewer` (`python -m dataagent.viewer`).

- [ ] **Step 1: Copy the two viewer modules + test**

```bash
SRC="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
DST="/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
cp "$SRC/tools/node_trace/catalog_view.py" "$DST/dataagent/catalog_view.py"
cp "$SRC/tools/node_trace/catalog_page.py" "$DST/dataagent/catalog_page.py"
cp "$SRC/tests/test_node_trace/test_catalog_view.py" "$DST/tests/test_catalog_view.py"
```

- [ ] **Step 2: Rewrite peer imports in `dataagent/catalog_view.py`**

Edit lines 13–14:
- `from datalayer.provenance import Provenance` → `from dataagent.provenance import Provenance`
- `import datalayer.context_dict as cd` → `import dataagent.context_dict as cd`

Leave line 11 (`from datalayer.catalog import DataCatalog`) and line 12 (`from datalayer.gateway import LocalDataGateway`) unchanged.

- [ ] **Step 3: Repoint paths + reconcile command in `dataagent/catalog_page.py`**

Add `from dataagent import paths`. Replace the `app.config.setdefault` catalog path defaults (the `"config/data_profiles"`, `"context"`, `"config/data_profiles/.provenance.json"` literals) with `str(paths.profiles_dir())`, `str(paths.context_dir())`, `str(paths.provenance_path())` respectively. Change the subprocess command (line ~525):
- old: `cmd = [sys.executable, "-m", "datalayer.sync", "--reconcile", "--json", tmp_json_path]`
- new: `cmd = [sys.executable, "-m", "dataagent.sync", "--reconcile", "--json", tmp_json_path]`

Update the module docstring's `datalayer.sync` reference → `dataagent.sync`. Keep the `CATALOG_RECONCILE_ENABLE` gate and autoescape exactly as-is.

- [ ] **Step 4: Write the standalone host `dataagent/viewer.py`**

```python
"""Standalone Flask host for the /catalog curation UI.

    python -m dataagent.viewer        # serves http://127.0.0.1:5057/catalog

Replaces the route previously mounted on AgenticSys_v2's trace viewer. All
data paths resolve via dataagent.paths; reconcile is gated by
CATALOG_RECONCILE_ENABLE just as before.
"""
from __future__ import annotations

import os

from flask import Flask, redirect

from dataagent import paths
from dataagent.catalog_page import register_catalog_routes


def build_app() -> Flask:
    app = Flask(__name__)
    app.config.setdefault("PROFILE_DIR", str(paths.profiles_dir()))
    app.config.setdefault("CONTEXT_DIR", str(paths.context_dir()))
    app.config.setdefault("PROVENANCE_PATH", str(paths.provenance_path()))
    register_catalog_routes(app)

    @app.get("/")
    def _root():
        return redirect("/catalog")

    return app


if __name__ == "__main__":
    port = int(os.environ.get("CATALOG_VIEWER_PORT", "5057"))
    build_app().run(host="127.0.0.1", port=port)
```

- [ ] **Step 5: Rewrite test imports + add a host smoke test**

In `tests/test_catalog_view.py`, replace `datalayer.provenance`/`datalayer.context_dict` peer imports and any `tools.node_trace.catalog_view`/`catalog_page` imports with `dataagent.catalog_view`/`dataagent.catalog_page`/`dataagent.provenance`/`dataagent.context_dict`. Leave `datalayer.catalog`/`gateway` as-is. Append a host smoke test:

```python
def test_viewer_app_registers_catalog_route():
    from dataagent.viewer import build_app
    app = build_app()
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/catalog" in rules
```

- [ ] **Step 6: Run tests**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/DataAgent"
python -m pytest tests/test_catalog_view.py -v
```
Expected: PASS (original catalog_view tests + the new host smoke test).

- [ ] **Step 7: Full DataAgent suite + commit (with user go-ahead)**

```bash
python -m pytest -q   # whole DataAgent suite green against the sibling
git add -A && git commit -m "feat: move catalog viewer into dataagent with standalone host"
```

---

### Task 7: Remove the moved code from AgenticSys_v2 (consumer-only)

This is the ONLY task that changes AgenticSys_v2. Do it last, after DataAgent is fully green.

**Files:**
- Delete: `datalayer/reconcile.py`, `datalayer/sync.py`, `datalayer/context_dict.py`, `datalayer/provenance.py`, `datalayer/verify_snapshot.py`, `tools/node_trace/catalog_view.py`, `tools/node_trace/catalog_page.py`
- Delete: `tests/test_sync.py`, `tests/test_sync_interactive_demo.py`, `tests/test_datalayer/test_reconcile.py`, `tests/test_datalayer/test_reconcile_cli.py`, `tests/test_datalayer/test_context_dict.py`, `tests/test_datalayer/test_provenance.py`, `tests/test_datalayer/test_verify_snapshot.py`, `tests/test_node_trace/test_catalog_view.py`
- Modify: `tools/node_trace/viewer.py`

**Interfaces:**
- Consumes/keeps: `datalayer/adapter.py`, `catalog.py`, `gateway.py`, `agent_factories/data_manager_agent.py` (incl. `sync_catalog`), all data folders — UNCHANGED.

- [ ] **Step 1: Remove the `/catalog` wiring from `tools/node_trace/viewer.py`**

Delete: line 25 `from tools.node_trace.catalog_page import register_catalog_routes`; line 75 `register_catalog_routes(app)`; the nav link at line 316 `<a href="/catalog">Data Catalog</a>`; and the four `app.config.setdefault(...)` catalog path blocks (PROFILE_DIR / CONTEXT_DIR / PROVENANCE_PATH / RECONCILE_RESULTS, lines ~55–72). Leave the Traces view and all other viewer behavior intact.

- [ ] **Step 2: Verify the Traces viewer still imports and registers**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python -c "from tools.node_trace.viewer import build_app; app=build_app(); rules={r.rule for r in app.url_map.iter_rules()}; print('catalog removed:', '/catalog' not in rules); assert '/catalog' not in rules"
```
Expected: `catalog removed: True` (adjust to the actual `build_app`/factory name if different — confirm by reading the top of `viewer.py`).

- [ ] **Step 3: Delete the moved modules + tests**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git rm datalayer/reconcile.py datalayer/sync.py datalayer/context_dict.py \
       datalayer/provenance.py datalayer/verify_snapshot.py \
       tools/node_trace/catalog_view.py tools/node_trace/catalog_page.py \
       tests/test_sync.py tests/test_sync_interactive_demo.py \
       tests/test_datalayer/test_reconcile.py tests/test_datalayer/test_reconcile_cli.py \
       tests/test_datalayer/test_context_dict.py tests/test_datalayer/test_provenance.py \
       tests/test_datalayer/test_verify_snapshot.py \
       tests/test_node_trace/test_catalog_view.py
```

- [ ] **Step 4: Grep-gate — no dangling references to moved code**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
grep -rnE "datalayer\.(reconcile|sync|context_dict|provenance|verify_snapshot)|catalog_view|catalog_page|register_catalog_routes" \
  --include="*.py" . | grep -v "/docs/" || echo "CLEAN: no references to moved modules"
```
Expected: `CLEAN: no references to moved modules`. If any non-doc `.py` line appears (e.g. `notebooks/run_question_suite.py` importing a moved module), resolve it: notebook references to the reconcile CLI become `python -m dataagent.sync`; `sync_catalog` itself stays and uses `adapter`, so it needs no change.

- [ ] **Step 5: Run the AgenticSys_v2 suite (consumer stays green)**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python -m pytest tests/test_datalayer tests/test_node_trace tests/test_agent_factories/test_data_manager_agent.py tests/test_agent_factories/test_data_manager_reconcile.py tests/test_catalog_sync.py -q
```
Expected: PASS. `adapter`-based reconciliation (`reconcile_case`, `apply_diff`) and `sync_catalog` still work; no collection errors from deleted modules. (Pre-existing `matplotlib` collection errors in viz/server modules are unrelated.)

- [ ] **Step 6: Commit (AgenticSys_v2, with user go-ahead)**

```bash
git add -A
git commit -m "refactor: remove reconciliation subsystem (extracted to ../DataAgent plug-in)"
```

---

## Self-Review

**Spec coverage:**
- DataAgent code-only package + sibling import seam → Task 1. ✓
- All 5 reconciliation modules moved (context_dict, provenance, reconcile, sync, verify_snapshot) → Tasks 2–5. ✓
- Catalog viewer (both `catalog_view.py` and `catalog_page.py`) + standalone host → Task 6. ✓
- `dataagent.paths` resolver incl. `snapshot_root`; `__file__`-relative paths repointed → Tasks 1, 4, 5, 6. ✓
- Import rewrites `datalayer.* → dataagent.*` for peers only; staying modules untouched → Tasks 2–6. ✓
- Removal from AgenticSys_v2 + `/catalog` un-wiring + grep gate; `adapter`/`sync_catalog`/data kept → Task 7. ✓
- Tests move with modules; consumer suite stays green → Tasks 2–7. ✓
- One-way dependency enforced by the Task 7 grep gate. ✓

**Placeholder scan:** none — every edit is given as an exact old→new line from the gathered line numbers; line numbers are labeled "~" where the implementer should confirm against the live file before editing.

**Type consistency:** `dataagent.paths` resolver names (`agenticsys_root`, `profiles_dir`, `provenance_path`, `context_dir`, `data_tables_dir`, `snapshot_root`) are used identically in Tasks 1/4/5/6. Moved-module public APIs are unchanged, so sibling consumers and tests keep the same symbols.

**Note on line numbers:** they reflect the files at planning time; the implementer must re-read each file before editing and match on content, not blindly on line number (several files were touched in recent commits).
