# Design: Extract profile reconciliation into a DataAgent plug-in

**Date:** 2026-06-30
**Topic:** Move the offline profile-reconciliation / catalog-curation subsystem out of AgenticSys_v2 into a sibling `../DataAgent` repo that acts as a **code-only plug-in**. AgenticSys_v2 becomes a pure consumer of the clean data profiles + data tables it already holds.
**Status:** Approved design, pending spec review.

## Motivation

AgenticSys_v2 currently mixes two concerns:
- **Producing/curating** clean data profiles (LLM-driven reconciliation of per-case
  schema drift, threshold normalization, context matching, provenance, snapshots,
  and a `/catalog` curation UI).
- **Consuming** those profiles at runtime to answer reviewer questions (catalog +
  gateway + data tools + agents + server).

The producer concern is an **offline dev tool**, never invoked by the running app
except for one lightweight runtime step. Splitting it out leaves AgenticSys_v2
focused on the agent Q&A runtime, and gives the curation tooling its own home.

## Decisions (locked during brainstorming)

1. **DataAgent is a code-only plug-in.** It contains the reconciliation/curation
   *code* and nothing else. **All data stays in AgenticSys_v2**: the data profiles
   (`config/data_profiles/*.yaml`), the provenance baseline
   (`config/data_profiles/.provenance.json`), the context source files
   (`context/*.txt`), and the data tables (`data_tables/...`). DataAgent reads and
   writes those folders in the sibling AgenticSys_v2 checkout.
2. **Runtime per-case sync stays here.** `datalayer/adapter.py` and its use in
   `server.py:_sync_case_catalog` (`adapter.reconcile_case` at ~line 829) remain in
   AgenticSys_v2 — the only runtime "reconciliation" is this lightweight,
   pandas-only, no-LLM, no-disk step, and it stays untouched.
3. **The LLM/CLI reconciliation moves out:** `reconcile.py`, `sync.py`,
   `context_dict.py`, `provenance.py`, `verify_snapshot.py`.
4. **The catalog viewer moves out:** both `tools/node_trace/catalog_view.py` (builds
   the view) and `tools/node_trace/catalog_page.py` (registers the `/catalog` GET +
   `/catalog/reconcile` POST routes, the reconcile subprocess trigger, and the
   `CATALOG_RECONCILE_ENABLE` gate). Their wiring in `tools/node_trace/viewer.py`
   (the `register_catalog_routes` import + call, the "Data Catalog" nav tab, and the
   catalog path defaults in `app.config`) is removed; DataAgent hosts the curation UI
   standalone.
5. **Data tables + DataGenerator stay as-is** in AgenticSys_v2 (simulation
   unchanged). Out of scope for this extraction.
6. **Shared modules are imported from the sibling repo.** Reconciliation depends on
   `datalayer.catalog`, `datalayer.gateway`, `datalayer.adapter`, `llm.*`,
   `logger.*`, `models.*` — all of which STAY in AgenticSys_v2. DataAgent puts
   AgenticSys_v2 on its import path (editable install or `.pth`) and imports them
   from there. No duplication, no vendoring.
7. **New top-level package name `dataagent/`** in DataAgent — the moved modules do
   NOT reuse the `datalayer` package name (which now resolves to the sibling repo's
   package).

## Architecture

```
   ../DataAgent  (code-only plug-in, offline)          ../AgenticSys_v2  (runtime app + all data)
   ┌─────────────────────────────────┐                 ┌───────────────────────────────────────┐
   │ dataagent/                      │  imports (path) │ datalayer/catalog.py  (DataCatalog)    │
   │   reconcile.py                  │ ───────────────▶│ datalayer/gateway.py  (LocalDataGateway)│
   │   sync.py        (CLI)          │                 │ datalayer/adapter.py  (runtime sync)   │
   │   context_dict.py               │                 │ llm/  logger/  models/                 │
   │   provenance.py                 │                 ├───────────────────────────────────────┤
   │   verify_snapshot.py (CLI)      │  reads + writes │ DATA (stays here, committed):          │
   │   catalog_view.py + viewer host │ ───────────────▶│  config/data_profiles/*.yaml           │
   │ tests/                          │                 │  config/data_profiles/.provenance.json │
   │ (NO data files)                 │                 │  context/*.txt                         │
   └─────────────────────────────────┘                 │  data_tables/real|simulated/<case>/    │
                                                        └───────────────────────────────────────┘
   AgenticSys_v2 has ZERO knowledge of DataAgent. The dependency is one-way:
   DataAgent → AgenticSys_v2 (code import + data folder read/write), offline only.
```

### Component 1 — DataAgent package layout

```
../DataAgent/
  dataagent/
    __init__.py
    reconcile.py          # was datalayer/reconcile.py
    sync.py               # was datalayer/sync.py        (python -m dataagent.sync)
    context_dict.py       # was datalayer/context_dict.py
    provenance.py         # was datalayer/provenance.py
    verify_snapshot.py    # was datalayer/verify_snapshot.py (python -m dataagent.verify_snapshot)
    catalog_view.py       # was tools/node_trace/catalog_view.py
    catalog_page.py       # was tools/node_trace/catalog_page.py (/catalog routes + reconcile trigger)
    viewer.py             # NEW: minimal standalone Flask host that calls register_catalog_routes
    paths.py              # NEW: resolves target data dirs in the sibling repo
  tests/                  # moved reconciliation/viewer tests
  conftest.py             # puts AgenticSys_v2 on sys.path for tests
  pyproject.toml          # declares dataagent; depends (editable) on AgenticSys_v2
  README.md               # setup: pip install -e ../AgenticSys_v2
```

### Component 2 — Import rewrites in the moved modules

For each moved file, imports split into two classes:

- **Stay (resolve from sibling AgenticSys_v2), unchanged:**
  `from datalayer.catalog import DataCatalog`, `from datalayer.gateway import ...`,
  `from datalayer import adapter`, `from llm...`, `from logger...`, `from models...`.
- **Rewrite (moved peers): `datalayer.* → dataagent.*`:**
  - `sync.py`: `from datalayer.reconcile import reconcile, ReconcileResult` →
    `from dataagent.reconcile import ...`; same for `context_dict`, `provenance`.
  - `reconcile.py`: imports of `context_dict` / `provenance` → `dataagent.*`.
  - `catalog_view.py`: `from datalayer.provenance import Provenance` →
    `from dataagent.provenance import Provenance`; `import datalayer.context_dict`
    → `import dataagent.context_dict`. (`from datalayer.catalog import DataCatalog`
    stays — resolved from sibling.)
  - `verify_snapshot.py`: any peer imports → `dataagent.*`.

### Component 3 — Target-path resolution (`dataagent/paths.py`)

DataAgent operates on AgenticSys_v2's data folders. A single resolver centralizes
where they are, defaulting to the sibling checkout and overridable by env var:

```python
# dataagent/paths.py
import os
from pathlib import Path

_DEFAULT_SIBLING = Path(__file__).resolve().parent.parent.parent / "AgenticSys_v2"

def agenticsys_root() -> Path:
    return Path(os.environ.get("AGENTICSYS_ROOT", _DEFAULT_SIBLING)).resolve()

def profiles_dir() -> Path:   return agenticsys_root() / "config" / "data_profiles"
def provenance_path() -> Path:return profiles_dir() / ".provenance.json"
def context_dir() -> Path:    return agenticsys_root() / "context"
def data_tables_dir() -> Path:return agenticsys_root() / "data_tables"
def snapshot_root() -> Path:  return agenticsys_root() / ".catalog_verified"
```

Critically, the moved modules currently compute data paths from their own
`__file__` (e.g. `sync.py`'s `_REPO_ROOT = Path(__file__).parent.parent`,
`verify_snapshot.py`'s `_DEFAULT_*` repo-relative strings). Once relocated under
`dataagent/`, those would resolve to the DataAgent repo, not AgenticSys_v2 — so each
such path MUST be repointed through `dataagent.paths`.

The moved CLIs (`sync.py`, `verify_snapshot.py`) and `catalog_view.py` source their
paths from `dataagent.paths` instead of hardcoded repo-relative paths. Where the
original code already accepts explicit path args (e.g. `context_dir` parameters,
profile output dirs), those are wired to default from `dataagent.paths`.

### Component 4 — Catalog viewer host (`dataagent/viewer.py`)

The `/catalog` page currently piggybacks on AgenticSys_v2's trace viewer
(`tools/node_trace/viewer.py`), which renders `catalog_view.build_catalog_view()`
and exposes a reconcile-trigger endpoint. DataAgent provides a minimal standalone
host that:
- serves the catalog page from `catalog_view.build_catalog_view()`,
- preserves the existing autoescape hardening and the `CATALOG_RECONCILE_ENABLE`
  gate on the reconcile-trigger endpoint (the trigger now shells `python -m
  dataagent.sync`),
- reads all data via `dataagent.paths`.

### Component 5 — Removals from AgenticSys_v2

- Delete `datalayer/reconcile.py`, `sync.py`, `context_dict.py`, `provenance.py`,
  `verify_snapshot.py`, `tools/node_trace/catalog_view.py`.
- In `tools/node_trace/viewer.py`: remove the `/catalog` route registration, the
  cross-nav "Data Catalog" tab, the reconcile-trigger endpoint, and the
  `CATALOG_RECONCILE_ENABLE` handling. The Traces view is unchanged.
- Remove the now-orphaned reconciliation tests (they move to DataAgent):
  `tests/test_datalayer/test_reconcile*.py`, `test_context_dict.py`,
  `test_provenance.py`, `test_sync.py`, `test_verify_snapshot.py`,
  `tests/test_agent_factories/test_data_manager_reconcile.py`,
  `tests/test_node_trace/test_catalog_view.py` (exact set confirmed during planning).
- `agent_factories/data_manager_agent.py`: **keep `sync_catalog()` and its `adapter`
  import** — it uses the staying `adapter.reconcile_case`/`apply_diff` and has a
  non-test caller (`notebooks/run_question_suite.py`). Its tests
  (`test_data_manager_agent.py`, `test_data_manager_reconcile.py`,
  `test_catalog_sync.py`) stay too.
- **Keep** `datalayer/adapter.py`, `catalog.py`, `gateway.py`, and the
  `config/data_profiles/*.yaml`, `.provenance.json`, `context/*.txt`, `data_tables/`
  data unchanged.

## Data flow

**Offline curation (in DataAgent):**
```
python -m dataagent.sync
  → reads  ../AgenticSys_v2/context/*.txt            (via dataagent.paths)
           ../AgenticSys_v2/data_tables/...
           ../AgenticSys_v2/config/data_profiles/*.yaml + .provenance.json
  → runs   LLM reconciliation (using sibling catalog/gateway/adapter/llm)
  → writes ../AgenticSys_v2/config/data_profiles/*.yaml + .provenance.json
  → (optionally) context/*.txt reverse-sync writes back into ../AgenticSys_v2/context/
You then commit the changed files inside AgenticSys_v2.
```

**Runtime (in AgenticSys_v2) — unchanged:**
```
DataCatalog() loads config/data_profiles/*.yaml
server _sync_case_catalog → adapter.reconcile_case (per-case, in-memory)
agents/data_tools query gateway + catalog
No DataAgent import anywhere on this path.
```

## Error handling

- **Sibling not on path:** DataAgent import of `datalayer.catalog` etc. fails fast
  with a clear message in `dataagent/__init__` or `conftest`: instruct
  `pip install -e ../AgenticSys_v2` (or set `AGENTICSYS_ROOT`). Documented in README.
- **`AGENTICSYS_ROOT` points nowhere / data dirs missing:** `dataagent.paths`
  resolvers raise a descriptive error naming the resolved path, rather than writing
  to an unexpected location.
- **Reconcile writes:** unchanged from current behavior (atomic provenance save,
  no-op guards) — those already-tested behaviors move with the code.

## Testing

**DataAgent (`../DataAgent/tests/`):**
- Moved reconciliation/viewer tests run against the sibling via `conftest.py`
  (sys.path / editable install). Tests that touched real data files must use the
  existing isolation pattern (tmp dirs + guard fixtures) — already present in the
  moved tests; verify they parametrize on `dataagent.paths` so they never mutate the
  real sibling data.
- A smoke test: `python -m dataagent.sync --help` (or a dry-run) imports cleanly
  with the sibling on path, proving the seam.

**AgenticSys_v2:**
- After removals, the consumer suite stays green. Specifically: `DataCatalog` load,
  `adapter.reconcile_case` per-case sync, data_tools queries, and the trace viewer
  (Traces view) all pass with no reference to the removed `/catalog` route or moved
  modules.
- Update/trim viewer tests so none assert the `/catalog` tab/route.
- Confirm no remaining `from datalayer.reconcile|sync|context_dict|provenance|
  verify_snapshot` or `catalog_view` imports anywhere in AgenticSys_v2 (grep gate).

## Sequencing

This is two coordinated repos. Recommended order (refined in the plan):
1. **Scaffold DataAgent** (package, pyproject, conftest, README, `dataagent.paths`)
   and `pip install -e ../AgenticSys_v2` so the seam exists.
2. **Copy** the five modules + `catalog_view.py` into `dataagent/`, rewrite peer
   imports, add the viewer host, point at `dataagent.paths`. Move their tests. Get
   DataAgent green against the sibling.
3. **Remove** the moved modules, the `/catalog` wiring, and the orphaned tests from
   AgenticSys_v2. Get the consumer suite green.

Step 3 is the only one that changes AgenticSys_v2; do it last so the curation tool
is proven before the originals are deleted.

## Future direction (beyond this extraction — informs the seam, not current scope)

DataAgent's intended long-term role is to be the authority for **all per-table data
curation and access**, not just profile reconciliation:

- **Per-table profile maintenance** — DataAgent owns the lifecycle of each table's
  profile (the `*.yaml`), keeping it current as tables evolve. This extraction is
  the first step: it already makes DataAgent the producer of `config/data_profiles/`.
- **Data access / gateway ownership with per-user scoping** — eventually DataAgent
  also maintains *data access*: which tables (and columns/rows) a given user may
  query. Different users have different access to the underlying tables, so the
  gateway grows from today's single in-memory `LocalDataGateway` into a per-user,
  access-scoped data layer that DataAgent governs. This aligns with the existing
  "central-DB queries replace the simulated gateway" vision; per-user access is the
  added dimension.

**How this extraction stays forward-compatible (so the future move is cheap):**
- The dependency is strictly **one-way** (DataAgent → AgenticSys_v2). When the
  gateway becomes DataAgent-owned, the arrow simply extends to cover it; AgenticSys_v2
  keeps consuming a gateway *interface* without learning about DataAgent.
- `gateway.py` stays a **clean, single-responsibility module** with the existing
  `DataGateway` query surface (`set_case`, `list_tables`, `query`). A future per-user
  access layer wraps/replaces the implementation behind that same surface — so
  AgenticSys_v2's consumers (`data_tools`, agents) are insulated.
- `dataagent.paths` + the plug-in model generalize: a future DataAgent that serves
  access decisions would expose them through the same sibling/interface seam, not
  through new imports into AgenticSys_v2.

Nothing here is built now — it is the reason the current cut keeps the gateway
surface clean and the dependency one-way.

## Out of scope / YAGNI

- Moving data tables / DataGenerator / the simulated gateway ("simulation as is").
- Per-user / access-scoped gateway — future direction above; the current gateway
  stays single-tenant and in this repo.
- Turning shared modules into a third shared package — sibling import is sufficient.
- Any change to the runtime query path, agents, or `adapter.py`.
- Publishing DataAgent to a package index — it's a local sibling plug-in.

## Files touched

**New (DataAgent):** `dataagent/{__init__,reconcile,sync,context_dict,provenance,
verify_snapshot,catalog_view,viewer,paths}.py`, `tests/...`, `conftest.py`,
`pyproject.toml`, `README.md`.

**Modified (AgenticSys_v2):** `tools/node_trace/viewer.py` (drop `/catalog`),
possibly `agent_factories/data_manager_agent.py` (drop test-only `sync_catalog`).

**Deleted (AgenticSys_v2):** `datalayer/{reconcile,sync,context_dict,provenance,
verify_snapshot}.py`, `tools/node_trace/catalog_view.py`, and the moved tests.

**Unchanged data (AgenticSys_v2):** `config/data_profiles/*.yaml`,
`.provenance.json`, `context/*.txt`, `data_tables/...`.
