# Data Layer Rename + Conceptual Pull Signal — Design Spec

**Date:** 2026-04-25
**Depends on:** existing `data/` package (gateway/catalog/generator/adapter), existing `gateway/` package (firewall_stack/llm_factory), `orchestrator/orchestrator.py` balance step, `models/types.py` `FinalAnswer`, `skills/workflow/balancing.md`, `main.py` data-source selection.

## Goal

Reduce longstanding friction in the data layer by (a) renaming two Python packages whose names don't describe their contents and (b) extending the Balance step to emit a structured "insufficient data — would pull" signal when specialist/synthesis gaps materially limit the answer. No real data-pull backend is deployed; the signal is advisory.

Together, these changes make it clear at a glance (1) where each type of data lives on disk, (2) which Python package owns data access vs. LLM access, and (3) when the system thinks it needs data it doesn't have.

## Non-goals

- **Not** deploying a real data-pull backend. `DataPullRequest` is a structured advisory only; nothing executes it.
- **Not** changing the `DataGateway` ABC or adding a new concrete gateway. The existing `LocalDataGateway` (renamed from `SimulatedDataGateway`) handles both simulated and real CSV flavors via its existing `from_case_folders(data_dir)` factory.
- **Not** migrating data — the current `data_tables/` is empty (only `README.md`), so the new `data_tables/{simulated,real}/` subfolders are created empty.
- **Not** tightening `would_pull` into structured `table.column` identifiers. MVP keeps them as free-text phrases (same shape as existing `SpecialistOutput.data_gaps`). A future pass can promote to structured identifiers if the real pull backend lands.
- **Not** restructuring the iteration notebooks from the 2026-04-24 plan. Those four notebooks live upstream of Balance and don't see this change.

## §1 — Package and folder renames

```
data/        → datalayer/    (Python package; ~20 imports updated across repo)
gateway/     → llm/          (Python package; ~10 imports updated)
gateway/llm_factory.py → llm/factory.py   (drop redundant prefix)
data_tables/<case>/*.csv → data_tables/{simulated,real}/<case>/*.csv
```

**Class rename (within `datalayer/gateway.py`):**

```
SimulatedDataGateway → LocalDataGateway
```

`SimulatedDataGateway` stays as a deprecated alias bound to `LocalDataGateway` so any stale import outside this repo keeps working for one cycle. The alias is removed in a follow-up after external consumers (if any) migrate.

**`.gitignore`**: current `data_tables/*/` rule already ignores contents under subfolders, so no change needed. Verify at implementation time.

**Why rename `llm_factory.py` → `factory.py`:** the `llm.` import prefix already carries the "LLM" meaning, so `from llm.factory import build_llm` is cleaner than `from llm.llm_factory import build_llm`.

## §2 — Data source selection

`main.py` currently chooses between a single CSV folder and the generator. After this change:

**Sources** (priority order, highest first):
1. `data_tables/real/` — if non-empty, win.
2. `data_tables/simulated/` — if non-empty, win.
3. Generator — fallback when neither folder has cases.

**Selection flag:** `main.py --data-source {auto,real,simulated,generator}` (default: `auto`). `auto` resolves via the priority above. Explicit values force the choice; `real`/`simulated` error out if the folder is empty rather than silently falling back (so "real" never quietly becomes "generator").

**Resolver:** one ~15-line helper `_resolve_data_source(flag, project_root) -> tuple[str, Path | None]` lives inline in `main.py`. Returns `(source_name, csv_dir)` where `csv_dir` is `None` for the generator path. Not extracted to `datalayer/` — single caller, not worth a module.

**Notebook parity:** the four iteration notebooks get `DATA_SOURCE = "auto"` in their knobs cell (default). Cell 3's existing "CSV-first, generator fallback" block is replaced with a call to the same inline resolver copied into each notebook (duplication accepted per the iteration-notebooks spec's Approach-2 decision).

**Logger event:** when the source is chosen, emit `data_source` event with `{source: str, path: str | None, case_count: int}`.

## §3 — Insufficiency signal in the Balance step

### New type (`models/types.py`)

```python
class DataPullRequest(BaseModel):
    """Advisory signal that the Balance step emits when specialist/report
    coverage is insufficient to answer the reviewer's question with
    confidence. No live pull exists today — this documents what a future
    Data Agent would target.
    """
    needed: bool
    reason: str
    would_pull: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"]
```

### `FinalAnswer` addition

```python
class FinalAnswer(BaseModel):
    # ... existing fields ...
    data_pull_request: DataPullRequest | None = None
```

Default `None` preserves backwards-compat with existing callers and JSON fixtures.

### Balancing skill (`skills/workflow/balancing.md`)

Add one paragraph to the skill body instructing the LLM: after merging the answers, judge whether specialist `data_gaps`, the team's `open_conflicts`, and the report's `coverage` together indicate the answer is materially incomplete. If so, return an additional `data_pull_request` object in the JSON response with fields `needed`, `reason`, `would_pull` (list of free-text phrases describing data that would help), `severity` (`low`/`medium`/`high`). If not, omit the field or set `needed=false`.

The "too much missing" threshold lives in the skill prose — no Python threshold. Criteria the skill surfaces to the LLM: specialist reported gaps + report coverage < full + open conflicts driven by missing evidence = indicators for `needed=true`.

### Balance parsing (`orchestrator/orchestrator.py`)

`Orchestrator.balance()`:
- After parsing `answer` and `flags`, also read `data_pull_request` dict.
- If present and well-formed, construct `DataPullRequest` and attach to `FinalAnswer`. Bad or missing field → set `data_pull_request=None`.
- Deterministic fallback (`_balance_fallback`) sets `data_pull_request=None`.

### Logger event

When `data_pull_request.needed == True`, emit:

```python
logger.log("data_pull_requested", {
    "would_pull": dpr.would_pull,
    "severity": dpr.severity,
    "reason": dpr.reason,
})
```

Matches the shape of the existing `data_gap_flagged` event.

## §4 — Surfacing in the final answer

### `ChatAgent.format_final_answer(final)` (`orchestrator/chat_agent.py`)

If `final.data_pull_request` is non-None and `needed=True`, append after the answer body:

```
---
**Data pull recommendation** (severity: {severity})

Reason: {reason}

Would pull: {comma-separated would_pull items, or "(nothing specific flagged)" if empty}

> No live pull today — the Data Agent is not deployed yet. This is a signal of what a future pull would target.
```

### Flag prepending

When `needed=True`, prepend `"data insufficient — pull recommended"` to `final.flags` so any flag-rendering code picks it up.

### No new CLI / no auto-execution

The pull is advisory. No `--enable-pull`, no queue file, no retry loop. The render is the output.

## §5 — Testing strategy

Renames and source selection get ordinary unit tests. The insufficiency signal is LLM-dependent, so it gets:

- **Unit tests for `DataPullRequest` parsing** in `Orchestrator.balance()` — feed a mock LLM result with/without the new field, assert the output `FinalAnswer.data_pull_request` matches.
- **Unit tests for `_balance_fallback`** — confirm `data_pull_request=None` on the fallback path.
- **Unit test for `format_final_answer`** — feed a `FinalAnswer` with and without a `DataPullRequest`, assert the formatted string contains / doesn't contain the "Data pull recommendation" block.
- **Integration test** (optional if LLM calls add too much cost to CI) — real `balance()` against hand-crafted `ReportDraft` + `TeamDraft` with heavy gaps, assert the real LLM emits `needed=true`.

Renames: run the full existing test suite and verify it passes (import paths updated automatically via grep-and-replace).

## §6 — Open questions

None — all scoping questions resolved during brainstorm. Future tightenings (e.g., promoting `would_pull` to structured `table.column` identifiers) are called out as non-goals for this MVP.
