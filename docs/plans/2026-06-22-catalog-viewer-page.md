# Catalog Viewer Page Implementation Plan (slice 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A `/catalog` page on the existing trace-viewer Flask app (port 3002) that monitors profiles/context/provenance and triggers `--reconcile` (subprocess) with structured results.

**Architecture:** New module `tools/node_trace/catalog_page.py` registers routes on the existing `app`; a pure `build_catalog_view` builder assembles the display model from cheap file reads; the trigger shells out to `python -m datalayer.sync --reconcile --json <tmp>` and reads the structured result.

**Tech Stack:** Python 3.11, Flask (`render_template_string`, mirroring viewer.py), PyYAML, pytest. Builds on `DataCatalog`, `context_dict`, `Provenance`, `run_reconcile`.

## Global Constraints
- Python 3.11; pandas only in `datalayer/adapter.py` — new code pure-Python.
- The reconcile trigger runs as a SUBPROCESS (never in-process LLM in the viewer).
- Tests must use isolated tmp dirs; the autouse guard fixture in `tests/test_datalayer/conftest.py` fails on real-`context/` mutation. Route/CLI tests MUST mock the subprocess (no real reconcile, no LLM).
- HTML mirrors viewer.py's existing inline style; no new CSS framework.
- Provenance badge semantics: a field present in `.provenance.json` with baseline == current → `agent`; present and differing → `human`; absent → `unmanaged`.

---

### Task V1: `--json` structured output on the reconcile CLI

**Files:** Modify `datalayer/sync.py`; Test `tests/test_datalayer/test_reconcile_cli.py`.

**Interfaces:** Produces a `--json <path>` arg on the `--reconcile` flow; after `run_reconcile`, writes `{"writes": [[t,c,f],...], "context_writes": [[t,v],...], "flags": [str,...]}` to `<path>`. `run_reconcile` already returns `ReconcileResult{writes, context_writes, flags}`.

- [ ] **Step 1: Failing test**
```python
# tests/test_datalayer/test_reconcile_cli.py (append) — reuse the existing run_reconcile fixture pattern
@pytest.mark.asyncio
async def test_run_reconcile_json_dump(tmp_path, monkeypatch):
    # build tmp data+profile+context as in test_run_reconcile_writes_threshold_from_context, monkeypatch CONTEXT_TABLE_MAP
    # ... (mirror that fixture) ...
    from datalayer.sync import run_reconcile, dump_result_json
    res = await run_reconcile(str(data), str(ctx), str(prof), llm=None)
    out = tmp_path / "result.json"
    dump_result_json(res, str(out))
    import json
    loaded = json.loads(out.read_text())
    assert set(loaded) == {"writes", "context_writes", "flags"}
    assert isinstance(loaded["flags"], list)
```
- [ ] **Step 2: Run — FAIL** (`ImportError: dump_result_json`).
- [ ] **Step 3: Implement** — add to `datalayer/sync.py`:
```python
def dump_result_json(result, path: str) -> None:
    import json
    payload = {
        "writes": [list(w) for w in result.writes],
        "context_writes": [list(c) for c in result.context_writes],
        "flags": list(result.flags),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
```
Then add a `--json` argparse arg to the `--reconcile` branch of `amain` and call `dump_result_json(result, args.json)` when `args.json` is set (after `run_reconcile`).
- [ ] **Step 4: Run — PASS**, plus full `tests/test_datalayer/ -q`.
- [ ] **Step 5: Commit** `feat(reconcile): --json structured output for the CLI`

---

### Task V2: `build_catalog_view` + provenance ownership helper

**Files:** Create `tools/node_trace/catalog_view.py`; Modify `datalayer/provenance.py` (add `ownership`); Test `tests/test_node_trace/test_catalog_view.py`.

**Interfaces:**
- `Provenance.ownership(table, col, field, current_value) -> str` → `"human"|"agent"|"unmanaged"` (`unmanaged` when no baseline recorded; `agent` when baseline == current; `human` otherwise).
- `build_catalog_view(profile_dir, context_dir, provenance_path) -> dict` → `{"tables": [ {table, description, aliases, columns: [{name, dtype, parse_hint, description, threshold, provenance}], context_only: [var,...]} ... ]}`. `provenance` per column aggregates its fields (`human` if any field human; else `agent` if any agent; else `unmanaged`). `threshold` is `{value, direction}` or None. A column's "has context entry" is reflected by a `in_context: bool` on each column.

- [ ] **Step 1: Failing test**
```python
# tests/test_node_trace/test_catalog_view.py
import yaml, json
from datalayer.provenance import Provenance
from tools.node_trace.catalog_view import build_catalog_view

def test_build_catalog_view(tmp_path, monkeypatch):
    prof = tmp_path/"prof"; prof.mkdir()
    (prof/"model_scores.yaml").write_text(yaml.safe_dump({"table":"model_scores","description":"d","columns":{
        "credit_loss_prob":{"dtype":"float","description":"x","risk_threshold":[10.0,100.0],"risk_direction":"range"}}}))
    ctx = tmp_path/"ctx"; ctx.mkdir()
    (ctx/"modeling_context_description.txt").write_text("1. credit_loss_prob: x. Scores from 10-100 are risky.\n2. extra_var: y.\n")
    import datalayer.context_dict as cd
    monkeypatch.setattr(cd, "CONTEXT_TABLE_MAP", {"modeling":["model_scores"]})
    pv = Provenance(str(tmp_path/".prov.json"))
    pv.record("model_scores","credit_loss_prob","description","x"); pv.save()   # agent-owned (baseline==current)

    view = build_catalog_view(str(prof), str(ctx), str(tmp_path/".prov.json"))
    t = view["tables"][0]
    assert t["table"] == "model_scores"
    col = t["columns"][0]
    assert col["name"] == "credit_loss_prob" and col["in_context"] is True
    assert col["provenance"] == "agent"
    assert "extra_var" in t["context_only"]
```
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `ownership` in provenance.py:
```python
    def ownership(self, table, col, field, current_value) -> str:
        baseline = self._data.get(table, {}).get(col, {})
        if field not in baseline:
            return "unmanaged"
        return "agent" if baseline[field] == current_value else "human"
```
Then `tools/node_trace/catalog_view.py` `build_catalog_view`: load `DataCatalog(profile_dir)`, `context_dict.load_context_by_table(context_dir)`, `Provenance(provenance_path)`; per table build columns (dtype/desc/threshold from the profile spec; `in_context` = name in the table's context map; provenance = aggregate `ownership` over `description`+`risk_threshold` fields); `context_only` = context vars not in the profile columns. Pure-Python; no pandas.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `feat(viewer): build_catalog_view + provenance ownership helper`

---

### Task V3: `/catalog` page + reconcile trigger

**Files:** Create `tools/node_trace/catalog_page.py`; Test `tests/test_node_trace/test_catalog_page.py`.

**Interfaces:** `register_catalog_routes(app)` adds `GET /catalog` (renders `build_catalog_view` as HTML via `render_template_string`, mirroring viewer.py style, with a Traces⇄Catalog nav and a provenance-badge legend + a Reconcile button with a `--no-llm` checkbox) and `POST /catalog/reconcile` (runs `subprocess.run(["python","-m","datalayer.sync","--reconcile","--json",<tmp>] + (["--no-llm"] if requested))`, reads the json, persists a summary to a results path, returns the rendered results / redirects to `/catalog`). Profile/context/provenance/results paths come from app config with sensible defaults (`config/data_profiles`, `context`, `config/data_profiles/.provenance.json`, `logs/last_reconcile.json`).

- [ ] **Step 1: Failing test** (Flask test client; subprocess MOCKED)
```python
# tests/test_node_trace/test_catalog_page.py
from flask import Flask
from unittest.mock import patch
from tools.node_trace.catalog_page import register_catalog_routes

def _app(tmp_path):
    app = Flask(__name__)
    app.config.update(PROFILE_DIR=str(tmp_path/"prof"), CONTEXT_DIR=str(tmp_path/"ctx"),
                      PROVENANCE_PATH=str(tmp_path/".prov.json"), RECONCILE_RESULTS=str(tmp_path/"last.json"))
    register_catalog_routes(app)
    return app

def test_catalog_get_renders(tmp_path, monkeypatch):
    (tmp_path/"prof").mkdir(); (tmp_path/"ctx").mkdir()
    import yaml
    (tmp_path/"prof"/"t.yaml").write_text(yaml.safe_dump({"table":"t","description":"d","columns":{"a":{"dtype":"int","description":"x"}}}))
    c = _app(tmp_path).test_client()
    r = c.get("/catalog")
    assert r.status_code == 200 and b"t" in r.data and b"Catalog" in r.data

def test_catalog_reconcile_invokes_subprocess(tmp_path):
    (tmp_path/"prof").mkdir(); (tmp_path/"ctx").mkdir()
    app = _app(tmp_path)
    with patch("tools.node_trace.catalog_page.subprocess.run") as m:
        # simulate the CLI writing the --json file
        def _fake(cmd, **kw):
            jpath = cmd[cmd.index("--json")+1]
            import json; open(jpath,"w").write(json.dumps({"writes":[],"context_writes":[],"flags":["[coverage] t.a"]}))
            class R: returncode=0; stdout=""; stderr=""
            return R()
        m.side_effect = _fake
        r = app.test_client().post("/catalog/reconcile", data={})
        assert r.status_code in (200, 302)
        assert m.called
        # results persisted
        import json, os
        assert os.path.exists(tmp_path/"last.json")
```
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `catalog_page.py` — `import subprocess`, the two routes, inline HTML template (table cards; per-column row with dtype/desc/threshold + a colored provenance badge; ✓/✗ context column; context-only list; last-run flags panel; Reconcile form posting to `/catalog/reconcile` with a `no_llm` checkbox). The POST handler builds the command, runs it with a timeout, reads the `--json` temp file, writes a summary to `RECONCILE_RESULTS`, and redirects to `/catalog` (PRG). Keep all paths from `app.config`.
- [ ] **Step 4: Run — PASS**, plus `tests/test_node_trace/ -q`.
- [ ] **Step 5: Commit** `feat(viewer): /catalog dashboard + reconcile trigger (subprocess)`

---

### Task V4: Wire into the trace viewer + cross-nav

**Files:** Modify `tools/node_trace/viewer.py`; Test `tests/test_node_trace/test_catalog_page.py` (append) or a small viewer wiring test.

**Interfaces:** `viewer.py` calls `register_catalog_routes(app)` once (after `app` is created and configured), passing through the profile/context/provenance/results paths via `app.config` (defaults as above, overridable by env). The existing trace pages get a top-nav link to `/catalog`.

- [ ] **Step 1: Failing test**
```python
# tests/test_node_trace/test_catalog_page.py (append)
def test_viewer_registers_catalog_route():
    import importlib
    from tools.node_trace import viewer
    importlib.reload(viewer)
    rules = {r.rule for r in viewer.app.url_map.iter_rules()}
    assert "/catalog" in rules
```
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — in `viewer.py`, after `app = Flask(__name__)` and config, `from tools.node_trace.catalog_page import register_catalog_routes; register_catalog_routes(app)`. Set the catalog config defaults on `app.config` (env-overridable). Add a `<a href="/catalog">Catalog</a>` link into the existing nav/header of the trace pages (keep it minimal). Confirm `python -m tools.node_trace.viewer` still starts and `import server` stays clean (server auto-launches this viewer).
- [ ] **Step 4: Run — PASS**, plus `python -c "import server"` and `python -c "import tools.node_trace.viewer"`.
- [ ] **Step 5: Commit** `feat(viewer): register /catalog on the trace viewer + cross-nav`

---

## Self-Review
- **Spec coverage:** monitoring view (V2+V3), provenance badges (V2), context coverage/context-only (V2), reconcile trigger as subprocess + `--json` (V1+V3), nav + same-port (V4). ✔
- **Placeholders:** V1/V3 tests say "mirror the existing fixture" for data setup — the implementer must write the fixture out (flagged, not hidden); the assertions are concrete.
- **Types:** `ReconcileResult.context_writes` (slice 2) consumed by V1; `build_catalog_view` dict shape consumed by V3; `Provenance.ownership` added V2, used V2/V3.
- **Safety:** all reconcile execution is subprocess + mocked in tests; no real `context/` touched (guard fixture); no in-process LLM.
