# Profile Reconciliation Agent — Slice 2 (Bidirectional + Catalog Reload) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `--reconcile` bidirectional — human-owned profile edits sync back to context txt, real columns with no profile entry are flagged — and let the running server's catalog auto-reflect profile changes.

**Architecture:** Provenance decides direction per field (forward = agent-owned/new, reverse = human-owned) so the two directions never conflict. Reverse-sync rewrites the existing context-txt line for a human-owned var (description + a threshold sentence re-rendered from the structured value); it never fabricates new dictionary lines. The server reloads the catalog from disk per case when a profile file's mtime changed.

**Tech Stack:** Python 3.11, PyYAML, pytest. Builds on slice 1 (`datalayer/context_dict.py`, `reconcile.py`, `provenance.py`, `catalog.py`).

## Global Constraints
- Python ≥ 3.11.9; keep openai-agents 0.15.1 / openai 2.30.0.
- pandas only in `datalayer/adapter.py` (enforced by `tests/test_adapter.py::test_pandas_scope`). New code stays pure-Python.
- Profiles written only via `catalog.write_profile_patch`; context txt written by the new `update_context_entry`.
- **Single-context-file coverage only:** a table covered by >1 context file is skipped for reverse-sync with a `[multi-context]` flag — never guess the target file.
- Reverse-sync writes a context line ONLY for a var that already has a line in its single context file; otherwise emit `[context-gap]`. Never fabricate dictionary entries.
- Threshold values are gold: `render_threshold` only renders the structured value already stored; it invents nothing.
- Flag taxonomy adds: `[coverage]`, `[context-gap]`, `[multi-context]` (alongside slice-1 `[schema-divergence]`/`[context-only]`/`[table-only]`/`[unresolved]`/`[human-owned]`).

---

### Task E1: `render_threshold` — structured threshold → sentence

**Files:**
- Modify: `datalayer/context_dict.py` (append)
- Test: `tests/test_datalayer/test_context_dict.py` (append)

**Interfaces:**
- Produces: `render_threshold(threshold: dict | None) -> str` — inverse of `normalize_threshold`. Returns `""` for None.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_datalayer/test_context_dict.py (append)
import pytest
from datalayer.context_dict import render_threshold

@pytest.mark.parametrize("thr,expected", [
    ({"risk_threshold": 5.8, "risk_direction": "above"}, "Values above 5.8 are risky."),
    ({"risk_threshold": 0.46, "risk_direction": "below"}, "Values below 0.46 are risky."),
    ({"risk_threshold": [10.0, 100.0], "risk_direction": "range"}, "Scores from 10 to 100 are risky."),
    (None, ""),
])
def test_render_threshold(thr, expected):
    assert render_threshold(thr) == expected
```
- [ ] **Step 2: Run — expect FAIL** (`ImportError: render_threshold`).
- [ ] **Step 3: Implement**
```python
# datalayer/context_dict.py (append)
def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)

def render_threshold(threshold: dict | None) -> str:
    if not threshold:
        return ""
    direction = threshold.get("risk_direction")
    value = threshold.get("risk_threshold")
    if direction == "range" and isinstance(value, (list, tuple)) and len(value) == 2:
        return f"Scores from {_fmt(value[0])} to {_fmt(value[1])} are risky."
    if direction in ("above", "below") and value is not None:
        return f"Values {direction} {_fmt(value)} are risky."
    return ""
```
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(reconcile): render_threshold (structured -> context sentence)`

---

### Task E2: Reverse map + `update_context_entry`

**Files:**
- Modify: `datalayer/context_dict.py` (append)
- Test: `tests/test_datalayer/test_context_dict.py` (append)

**Interfaces:**
- Consumes: `render_threshold` (E1), `CONTEXT_TABLE_MAP`.
- Produces:
  - `context_files_for_table(table: str) -> list[str]` — context-file stems whose `CONTEXT_TABLE_MAP` entry includes `table`.
  - `update_context_entry(context_dir, table, var_name, description, threshold) -> str` — returns one of: `"updated"` (rewrote the var's existing line in the table's single context file), `"not_found"` (single file, but no line for var_name), `"multi_context"` (table covered by >1 file → skipped), `"no_context"` (table maps to 0 files). On `"updated"` the file is rewritten preserving every other line; the target line becomes `"<n>. <var_name>: <description>. <render_threshold(threshold)>"` (trailing space + sentence omitted when threshold is None/empty).

- [ ] **Step 1: Write the failing test**
```python
# tests/test_datalayer/test_context_dict.py (append)
from datalayer.context_dict import update_context_entry, context_files_for_table

def _write(p, text): p.write_text(text); return str(p)

def test_update_context_entry_rewrites_existing_line(tmp_path, monkeypatch):
    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling": ["model_scores"]})
    f = tmp_path / "modeling_context_description.txt"
    f.write_text("1. credit_loss_prob: old desc. Scores from 10-100 are risky.\n"
                 "2. cbr_score: bureau score.\n")
    status = update_context_entry(str(tmp_path), "model_scores", "credit_loss_prob",
                                  "new clear desc", {"risk_threshold": 5.8, "risk_direction": "above"})
    assert status == "updated"
    lines = f.read_text().splitlines()
    assert lines[0] == "1. credit_loss_prob: new clear desc. Values above 5.8 are risky."
    assert lines[1] == "2. cbr_score: bureau score."   # other lines preserved

def test_update_context_entry_not_found(tmp_path, monkeypatch):
    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling": ["model_scores"]})
    (tmp_path / "modeling_context_description.txt").write_text("1. cbr_score: x.\n")
    assert update_context_entry(str(tmp_path), "model_scores", "credit_loss_prob", "d", None) == "not_found"

def test_update_context_entry_multi_context(tmp_path, monkeypatch):
    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"spend": ["spends"], "payment_spend": ["spends", "payments"]})
    assert update_context_entry(str(tmp_path), "spends", "amount", "d", None) == "multi_context"
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement**
```python
# datalayer/context_dict.py (append)
def context_files_for_table(table: str) -> list[str]:
    return [stem for stem, tables in CONTEXT_TABLE_MAP.items() if table in tables]

def update_context_entry(context_dir, table, var_name, description, threshold) -> str:
    stems = context_files_for_table(table)
    if not stems:
        return "no_context"
    if len(stems) > 1:
        return "multi_context"
    path = os.path.join(context_dir, f"{stems[0]}_context_description.txt")
    if not os.path.isfile(path):
        return "not_found"
    sentence = render_threshold(threshold)
    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    out, found = [], False
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) == var_name:
            idx = line.split(".", 1)[0].strip()
            desc = description.strip()
            body = f"{desc} {sentence}".strip() if sentence else desc
            out.append(f"{idx}. {var_name}: {body}\n")
            found = True
        else:
            out.append(line)
    if not found:
        return "not_found"
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)
    return "updated"
```
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(reconcile): context-txt writeback (single-file, update-existing-only)`

---

### Task E3: `DataCatalog.reload()` + `reload_if_changed()`

**Files:**
- Modify: `datalayer/catalog.py`
- Test: `tests/test_datalayer/test_catalog_reload.py`

**Interfaces:**
- Produces: `DataCatalog.reload() -> None` (re-runs `_load`, replacing `_profiles`); `DataCatalog.reload_if_changed() -> bool` (reloads + returns True only if any `*.yaml` mtime in `_profile_dir` is newer than the last load; tracks `self._loaded_mtime`).

- [ ] **Step 1: Write the failing test**
```python
# tests/test_datalayer/test_catalog_reload.py
import time, yaml
from datalayer.catalog import DataCatalog

def _profile(p, desc):
    (p / "t.yaml").write_text(yaml.safe_dump(
        {"table": "t", "description": desc, "columns": {"a": {"dtype": "int", "description": desc}}}))

def test_reload_picks_up_changed_yaml(tmp_path):
    _profile(tmp_path, "old")
    cat = DataCatalog(profile_dir=str(tmp_path))
    assert cat.get_description("t") == "old"
    time.sleep(0.01)
    _profile(tmp_path, "new")
    cat.reload()
    assert cat.get_description("t") == "new"

def test_reload_if_changed_only_when_mtime_advances(tmp_path):
    _profile(tmp_path, "old")
    cat = DataCatalog(profile_dir=str(tmp_path))
    assert cat.reload_if_changed() is False          # nothing changed since load
    time.sleep(0.01)
    _profile(tmp_path, "new")
    assert cat.reload_if_changed() is True            # mtime advanced
    assert cat.get_description("t") == "new"
    assert cat.reload_if_changed() is False           # stable again
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — record `self._loaded_mtime` at end of `_load`; add:
```python
    def _max_mtime(self) -> float:
        return max((p.stat().st_mtime for p in self._profile_dir.glob("*.yaml")), default=0.0)

    def reload(self) -> None:
        self._profiles = {}
        self._load()

    def reload_if_changed(self) -> bool:
        if self._max_mtime() > getattr(self, "_loaded_mtime", 0.0):
            self.reload()
            return True
        return False
```
(set `self._loaded_mtime = self._max_mtime()` at the end of `_load`.)
- [ ] **Step 4: Run — expect PASS.** Also run `tests/test_datalayer/ -k catalog`.
- [ ] **Step 5: Commit** `feat(catalog): reload() + mtime-gated reload_if_changed()`

---

### Task E4: Reverse-sync + coverage in `reconcile()`

**Files:**
- Modify: `datalayer/reconcile.py`
- Test: `tests/test_datalayer/test_reconcile.py` (append)

**Interfaces:**
- Consumes: `update_context_entry` (E2). `reconcile(...)` signature gains keyword `context_dir: str = "context"`.
- Produces: `ReconcileResult` gains `context_writes: list` (`(table, var)` pairs written back). Behavior change: in the provenance gate, when a field is **human-owned** (not agent-owned), instead of only emitting `[human-owned]`, the reconciler reads the live profile description+threshold and calls `update_context_entry`; the status maps to a flag: `updated` → record in `context_writes`; `not_found` → `[context-gap]`; `multi_context` → `[multi-context]`; `no_context` → `[context-gap]`. Coverage: a real column with no exact profile entry and no confident agent match emits `[coverage] <table>.<col> not covered by any profile entry` (in addition to the existing `[unresolved]`).

- [ ] **Step 1: Write the failing test**
```python
# tests/test_datalayer/test_reconcile.py (append)
@pytest.mark.asyncio
async def test_reconcile_reverse_syncs_human_field_to_context(tmp_path, monkeypatch):
    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling": ["model_scores"]})
    ctxdir = tmp_path / "context"; ctxdir.mkdir()
    (ctxdir / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: stale desc. Values above 1 are risky.\n")
    from datalayer.gateway import LocalDataGateway
    gw = LocalDataGateway(case_data={"c1": {"model_scores": [{"credit_loss_prob": "55"}]}})
    cat = _Catalog()  # from earlier tests
    cat._profiles["model_scores"]["columns"]["credit_loss_prob"]["description"] = "HUMAN DESC"
    from datalayer.provenance import Provenance
    pv = Provenance(str(tmp_path / ".prov.json"))
    pv.record("model_scores", "credit_loss_prob", "description", "agent-wrote-earlier")  # human diverged
    from datalayer.reconcile import reconcile
    ctx = {"model_scores": {"credit_loss_prob": __import__("datalayer.context_dict", fromlist=["ContextEntry"]).ContextEntry(
        "credit_loss_prob", "stale desc", None, threshold=None)}}
    res = await reconcile(gw, cat, _Agent(), ctx, pv, context_dir=str(ctxdir))
    line = (ctxdir / "modeling_context_description.txt").read_text().splitlines()[0]
    assert "HUMAN DESC" in line                       # human edit pushed back to context
    assert ("model_scores", "credit_loss_prob") in res.context_writes
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — thread `context_dir` through `reconcile`; add `context_writes` to `ReconcileResult`; in the human-owned branch call `update_context_entry(context_dir, table, canonical, live_desc, live_threshold)` and map status→flag/record; add the `[coverage]` flag in the unresolved/no-profile-entry path. Keep slice-1 tests green (forward write still happens for agent-owned fields; `[human-owned]` flag still emitted alongside the reverse-sync attempt).
- [ ] **Step 4: Run** `python -m pytest tests/test_datalayer/test_reconcile.py tests/test_adapter.py::test_pandas_scope -v` — all pass.
- [ ] **Step 5: Commit** `feat(reconcile): bidirectional reverse-sync + coverage flags`

---

### Task E5: Server catalog auto-reload per case + CLI passthrough

**Files:**
- Modify: `server.py` (one `reload_if_changed()` call per case)
- Modify: `datalayer/sync.py` (`run_reconcile` passes `context_dir` to `reconcile`)
- Test: `tests/test_datalayer/test_reconcile_cli.py` (append)

**Interfaces:**
- Consumes: `DataCatalog.reload_if_changed` (E3), `reconcile(..., context_dir=...)` (E4).

- [ ] **Step 1: Write the failing test** (CLI passthrough end-to-end: a human-owned field gets synced to a tmp context dir through `run_reconcile`)
```python
# tests/test_datalayer/test_reconcile_cli.py (append)
@pytest.mark.asyncio
async def test_run_reconcile_passes_context_dir_for_reverse_sync(tmp_path, monkeypatch):
    # build tmp data + profile + context; pre-edit the profile as a human, record divergent provenance,
    # run run_reconcile(llm=None), assert the context txt line now reflects the human profile value.
    ...  # full fixture mirrors test_run_reconcile_writes_threshold_from_context + a human edit
```
- [ ] **Step 2: Run — expect FAIL** (context_dir not threaded / not reverse-synced).
- [ ] **Step 3: Implement**
  - `sync.run_reconcile`: pass `context_dir=context_dir` into `reconcile(...)`.
  - `server.py`: at the start of each case (next to `_sync_case_catalog`, ~line 875), call `_CATALOG.reload_if_changed()` so a between-turns `--reconcile` is picked up. Catalog-only; do not touch any LLM/SSE emission.
- [ ] **Step 4: Run** `python -m pytest tests/test_datalayer/ tests/test_adapter.py -v` and `python -c "import server"` (import-clean).
- [ ] **Step 5: Commit** `feat(reconcile): server catalog auto-reload per case + CLI context_dir passthrough`

---

## Self-Review
- **Spec coverage:** bidirectional (E4) ✔; reverse map + writeback single-file (E2) ✔; render_threshold (E1) ✔; catalog reload (E3) + server wiring (E5) ✔; coverage/context-gap/multi-context flags (E4) ✔.
- **Placeholders:** E5 Step-1 test body is intentionally a fixture-sketch (mirrors an existing test) — the implementer must write it out; flagged explicitly, not a hidden gap.
- **Types:** `update_context_entry` returns the 4 string statuses E4 maps on; `render_threshold` shape matches `normalize_threshold`; `ReconcileResult.context_writes` added in E4, asserted in E4/E5.
- **Provenance direction:** reverse-sync fires exactly in the human-owned branch (provenance `is_agent_owned == False`), keeping forward/reverse on disjoint field sets.
