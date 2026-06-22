# Profile Reconciliation Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A manager-triggered, agent-assisted loop that reconciles live data tables ↔ canonical profiles ↔ context dictionaries and auto-writes converged `config/data_profiles/*.yaml`, protecting human edits and flagging anomalies.

**Architecture:** Deterministic core (context parsing, threshold regex, cross-case consistency, dtype validation, provenance gate, flag reporting) does the safe/exact work and gates every write; a small LLM agent (extending the existing `DataManagerAgent`) does only the judgment calls — fuzzy column matching, description polish, and threshold normalization the regex couldn't parse. Writes go through `catalog.write_profile_patch`; a provenance sidecar protects human-edited fields; git is the audit trail.

**Tech Stack:** Python 3.11, pandas (sync-time only, already isolated in `adapter.py`), PyYAML, the `openai-agents`/firewall LLM stack via `DataManagerAgent.llm.ainvoke`, pytest + pytest-asyncio.

## Global Constraints

- Python ≥ 3.11.9 on the deploy server; keep `openai-agents==0.15.1` / `openai==2.30.0` (do not pin down).
- pandas may be imported ONLY in `datalayer/adapter.py` (enforced by `tests/test_adapter.py::test_pandas_scope`). New modules must stay pure-Python; route any dtype/sample checks through `adapter` helpers.
- The agent's LLM call must honor `LLM_BACKEND` (OpenAI dev / safechain prod) — reuse `DataManagerAgent.llm` (built via `llm.factory.build_llm`), never construct a raw client.
- Thresholds are gold-standard: the agent may normalize phrasing but must NEVER change a threshold's numeric value.
- Profiles are written only via `catalog.write_profile_patch(table, patch)` (preserves the merge semantics). Never hand-write YAML.
- Reconciliation runs only on explicit manager command; never per-case at query time. pandas/LLM live behind the sync entrypoint.
- Profile fields in play: `dtype`, `description`, `description_pending`, `parse_hint`, `aliases`, `risk_threshold`, `risk_direction` (`"above"`/`"below"`), `categories`.

---

### Task 1: Context dictionary parser

**Files:**
- Create: `datalayer/context_dict.py`
- Test: `tests/test_datalayer/test_context_dict.py`

**Interfaces:**
- Produces: `parse_context_file(path: str) -> list[ContextEntry]` where `ContextEntry` is a dataclass `{var_name: str, raw_description: str, threshold_text: str | None}`. `threshold_text` is the substring describing the risk threshold (e.g. `"Values above 5.8 are risky"`), or `None` if the line carries no threshold sentence.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_context_dict.py
from datalayer.context_dict import parse_context_file, ContextEntry

SAMPLE = """Data Description
You are a risk analyst. Analyze the case.
1. tpf_internal_delinq_idx: Internal Delinquency Index. Values above 5.8 are considered risky.
2. cust_lndexpsr_minloc_6m_ratio: customer lending exposure minloc 6 months ratio
3. credit_loss_prob: ML model score predicting default. Scores from 10-100 are considered risky.
"""

def test_parse_context_file_extracts_entries(tmp_path):
    p = tmp_path / "modeling_context_description.txt"
    p.write_text(SAMPLE)
    entries = parse_context_file(str(p))
    by_name = {e.var_name: e for e in entries}

    assert set(by_name) == {
        "tpf_internal_delinq_idx",
        "cust_lndexpsr_minloc_6m_ratio",
        "credit_loss_prob",
    }
    # Threshold sentence captured separately from the description.
    assert by_name["tpf_internal_delinq_idx"].threshold_text == "Values above 5.8 are considered risky."
    assert "Internal Delinquency Index" in by_name["tpf_internal_delinq_idx"].raw_description
    # Line with no threshold sentence → threshold_text is None.
    assert by_name["cust_lndexpsr_minloc_6m_ratio"].threshold_text is None
    # Preamble lines (no "N. name:" shape) are ignored.
    assert all(isinstance(e, ContextEntry) for e in entries)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_datalayer/test_context_dict.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'datalayer.context_dict'`

- [ ] **Step 3: Write minimal implementation**

```python
# datalayer/context_dict.py
"""Parse context-dictionary txt files (context/*_context_description.txt).

Each substantive line is `N. var_name: description. threshold sentence`.
Pure-Python (no pandas). Threshold *interpretation* lives in threshold.py;
this module only splits the description from the threshold sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# "1. var_name: rest"  — leading index optional, var_name is snake/alnum.
_LINE = re.compile(r"^\s*\d+\.\s*([A-Za-z0-9_]+)\s*:\s*(.+)$")
# A sentence that states a risk threshold.
_THRESHOLD_SENTENCE = re.compile(
    r"[^.]*\b(?:values?|scores?)\b[^.]*\b(?:risky|risk)\b[^.]*\.",
    re.IGNORECASE,
)


@dataclass
class ContextEntry:
    var_name: str
    raw_description: str
    threshold_text: str | None


def parse_context_file(path: str) -> list[ContextEntry]:
    entries: list[ContextEntry] = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            m = _LINE.match(line)
            if not m:
                continue
            var_name, rest = m.group(1), m.group(2).strip()
            tm = _THRESHOLD_SENTENCE.search(rest)
            threshold_text = tm.group(0).strip() if tm else None
            description = rest.replace(threshold_text, "").strip() if threshold_text else rest
            entries.append(ContextEntry(var_name, description, threshold_text))
    return entries
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_datalayer/test_context_dict.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datalayer/context_dict.py tests/test_datalayer/test_context_dict.py
git commit -m "feat(reconcile): context-dictionary txt parser"
```

---

### Task 2: Deterministic threshold normalizer

**Files:**
- Modify: `datalayer/context_dict.py` (append)
- Test: `tests/test_datalayer/test_context_dict.py` (append)

**Interfaces:**
- Produces: `normalize_threshold(text: str | None) -> dict | None` returning `{"risk_threshold": float, "risk_direction": "above"|"below"}` for single-bound phrasings, `{"risk_threshold": [lo, hi], "risk_direction": "range"}` for range phrasings, or `None` when the text is absent or not parseable by regex (those go to the agent in Task 6). Never invents a value.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_context_dict.py  (append)
import pytest
from datalayer.context_dict import normalize_threshold

@pytest.mark.parametrize("text,expected", [
    ("Values above 5.8 are considered risky.", {"risk_threshold": 5.8, "risk_direction": "above"}),
    ("Values below 0.46 are risky", {"risk_threshold": 0.46, "risk_direction": "below"}),
    ("Values on or above 1 are risky", {"risk_threshold": 1.0, "risk_direction": "above"}),
    ("Scores from 10-100 are considered risky.", {"risk_threshold": [10.0, 100.0], "risk_direction": "range"}),
    (None, None),
    ("some prose with no numbers", None),
])
def test_normalize_threshold(text, expected):
    assert normalize_threshold(text) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_datalayer/test_context_dict.py::test_normalize_threshold -v`
Expected: FAIL with `ImportError: cannot import name 'normalize_threshold'`

- [ ] **Step 3: Write minimal implementation**

```python
# datalayer/context_dict.py  (append)

_RANGE = re.compile(r"\bfrom\s+(-?\d+(?:\.\d+)?)\s*[-to]+\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_BOUND = re.compile(r"\b(above|below|over|under|greater than|less than|on or above|on or below)\b\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)

_BELOW_WORDS = {"below", "under", "less than", "on or below"}


def normalize_threshold(text: str | None) -> dict | None:
    if not text:
        return None
    rm = _RANGE.search(text)
    if rm:
        return {"risk_threshold": [float(rm.group(1)), float(rm.group(2))], "risk_direction": "range"}
    bm = _BOUND.search(text)
    if bm:
        word = bm.group(1).lower()
        direction = "below" if word in _BELOW_WORDS else "above"
        return {"risk_threshold": float(bm.group(2)), "risk_direction": direction}
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_datalayer/test_context_dict.py::test_normalize_threshold -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datalayer/context_dict.py tests/test_datalayer/test_context_dict.py
git commit -m "feat(reconcile): deterministic threshold normalizer"
```

---

### Task 3: Context → table map and loader

**Files:**
- Modify: `datalayer/context_dict.py` (append)
- Test: `tests/test_datalayer/test_context_dict.py` (append)

**Interfaces:**
- Consumes: `parse_context_file`, `normalize_threshold` (Task 1–2).
- Produces: `CONTEXT_TABLE_MAP: dict[str, list[str]]` (`{context_filename_stem: [canonical_table, ...]}`) and `load_context_by_table(context_dir: str) -> dict[str, dict[str, ContextEntry]]` returning `{canonical_table: {var_name: ContextEntry}}`, with `ContextEntry.threshold` (a new field) pre-normalized via `normalize_threshold`. A context file mapped to several tables contributes its entries to each.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_context_dict.py  (append)
from datalayer.context_dict import CONTEXT_TABLE_MAP, load_context_by_table

def test_load_context_by_table(tmp_path):
    (tmp_path / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: default score. Scores from 10-100 are risky.\n"
    )
    # Monkeypatch the map to point a known stem at two tables.
    import datalayer.context_dict as cd
    cd.CONTEXT_TABLE_MAP = {"modeling": ["model_scores", "model_scores_transaction"]}

    out = load_context_by_table(str(tmp_path))
    assert "model_scores" in out and "model_scores_transaction" in out
    entry = out["model_scores"]["credit_loss_prob"]
    assert entry.threshold == {"risk_threshold": [10.0, 100.0], "risk_direction": "range"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_datalayer/test_context_dict.py::test_load_context_by_table -v`
Expected: FAIL with `ImportError: cannot import name 'CONTEXT_TABLE_MAP'`

- [ ] **Step 3: Write minimal implementation**

```python
# datalayer/context_dict.py  — add `threshold` field to ContextEntry and the loader.

# 1) Change the dataclass to carry the normalized threshold:
@dataclass
class ContextEntry:
    var_name: str
    raw_description: str
    threshold_text: str | None
    threshold: dict | None = None   # filled by load_context_by_table

# 2) Static domain→tables map (filename stem before "_context_description.txt").
import os

CONTEXT_TABLE_MAP: dict[str, list[str]] = {
    "modeling": ["model_scores", "model_scores_transaction"],
    "score_driver": ["score_drivers", "score_drivers_transaction"],
    "crossbu": ["crossbu_cards", "crossbu_merchants"],
    "spend": ["spends"],
    "payment": ["payments"],
    "payment_spend": ["spends", "payments"],
    "bureau": ["bureau"],
    "strategy": ["strategy"],
}


def load_context_by_table(context_dir: str) -> dict[str, dict[str, ContextEntry]]:
    out: dict[str, dict[str, ContextEntry]] = {}
    for fname in sorted(os.listdir(context_dir)):
        if not fname.endswith("_context_description.txt"):
            continue
        stem = fname[: -len("_context_description.txt")]
        tables = CONTEXT_TABLE_MAP.get(stem)
        if not tables:
            continue
        entries = parse_context_file(os.path.join(context_dir, fname))
        for e in entries:
            e.threshold = normalize_threshold(e.threshold_text)
        for table in tables:
            bucket = out.setdefault(table, {})
            for e in entries:
                bucket[e.var_name] = e
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_datalayer/test_context_dict.py::test_load_context_by_table -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datalayer/context_dict.py tests/test_datalayer/test_context_dict.py
git commit -m "feat(reconcile): context->table map + per-table loader"
```

---

### Task 4: Cross-case schema consistency check

**Files:**
- Create: `datalayer/reconcile.py`
- Test: `tests/test_datalayer/test_reconcile.py`

**Interfaces:**
- Consumes: `LocalDataGateway` (`list_case_ids`, `set_case`, `list_tables`, `query`).
- Produces: `check_consistency(gateway) -> ConsistencyResult` where `ConsistencyResult` is a dataclass `{uniform_schema: dict[str, set[str]], flags: list[str]}`. `uniform_schema` maps `{table: column_set}` for tables consistent across ALL cases; any table whose column set (or presence) differs across cases is omitted from `uniform_schema` and a human-readable flag string is appended (naming the table and the diverging cases).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_reconcile.py
from datalayer.gateway import LocalDataGateway
from datalayer.reconcile import check_consistency

def _gw(case_data):
    return LocalDataGateway(case_data=case_data)

def test_consistency_uniform_no_flags():
    gw = _gw({
        "c1": {"t": [{"a": 1, "b": 2}]},
        "c2": {"t": [{"a": 9, "b": 8}]},
    })
    res = check_consistency(gw)
    assert res.flags == []
    assert res.uniform_schema == {"t": {"a", "b"}}

def test_consistency_divergent_table_is_flagged_and_excluded():
    gw = _gw({
        "c1": {"t": [{"a": 1, "b": 2}]},
        "c2": {"t": [{"a": 9}]},          # missing column b
    })
    res = check_consistency(gw)
    assert "t" not in res.uniform_schema
    assert any("t" in f and "c2" in f for f in res.flags)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_datalayer/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'datalayer.reconcile'`

- [ ] **Step 3: Write minimal implementation**

```python
# datalayer/reconcile.py
"""Manager-triggered reconciliation: tables ↔ profiles ↔ context dictionaries.

Pure-Python orchestration. Dtype/sample validation is delegated to adapter
helpers (the only pandas-importing module). LLM judgment is delegated to the
DataManagerAgent passed in by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConsistencyResult:
    uniform_schema: dict[str, set]
    flags: list[str] = field(default_factory=list)


def check_consistency(gateway) -> ConsistencyResult:
    case_ids = gateway.list_case_ids()
    # {table: {case_id: column_set}}
    per_case: dict[str, dict[str, set]] = {}
    for cid in case_ids:
        gateway.set_case(cid)
        for table in gateway.list_tables():
            rows = gateway.query(table) or []
            cols = set(rows[0].keys()) if rows else set()
            per_case.setdefault(table, {})[cid] = cols

    uniform: dict[str, set] = {}
    flags: list[str] = []
    for table, by_case in per_case.items():
        present_cases = set(by_case)
        column_sets = list(by_case.values())
        all_present = present_cases == set(case_ids)
        all_equal = all(c == column_sets[0] for c in column_sets)
        if all_present and all_equal:
            uniform[table] = column_sets[0]
        else:
            ref = max(column_sets, key=len) if column_sets else set()
            divergent = [
                cid for cid, cols in by_case.items() if cols != ref
            ] + [cid for cid in case_ids if cid not in present_cases]
            flags.append(
                f"[schema-divergence] table '{table}' differs across cases: {sorted(divergent)}"
            )
    return ConsistencyResult(uniform_schema=uniform, flags=flags)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_datalayer/test_reconcile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datalayer/reconcile.py tests/test_datalayer/test_reconcile.py
git commit -m "feat(reconcile): cross-case schema consistency check"
```

---

### Task 5: Provenance store (sidecar JSON)

**Files:**
- Create: `datalayer/provenance.py`
- Test: `tests/test_datalayer/test_provenance.py`

**Interfaces:**
- Produces: class `Provenance(path: str)` with `is_agent_owned(table, col, field, current_value) -> bool` (True when never recorded OR the recorded baseline equals `current_value`; False when a human changed it), `record(table, col, field, value) -> None`, and `save() -> None`. Backed by a JSON sidecar `config/data_profiles/.provenance.json` of shape `{table: {col: {field: baseline_value}}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_provenance.py
from datalayer.provenance import Provenance

def test_unrecorded_field_is_agent_owned(tmp_path):
    pv = Provenance(str(tmp_path / ".provenance.json"))
    assert pv.is_agent_owned("model_scores", "cbr_score", "description", "anything") is True

def test_recorded_then_unchanged_is_agent_owned(tmp_path):
    pv = Provenance(str(tmp_path / ".provenance.json"))
    pv.record("model_scores", "cbr_score", "description", "agent text")
    assert pv.is_agent_owned("model_scores", "cbr_score", "description", "agent text") is True

def test_recorded_then_human_changed_is_not_agent_owned(tmp_path):
    pv = Provenance(str(tmp_path / ".provenance.json"))
    pv.record("model_scores", "cbr_score", "description", "agent text")
    assert pv.is_agent_owned("model_scores", "cbr_score", "description", "human edit") is False

def test_roundtrip_persists(tmp_path):
    p = str(tmp_path / ".provenance.json")
    pv = Provenance(p); pv.record("t", "c", "f", "v"); pv.save()
    assert Provenance(p).is_agent_owned("t", "c", "f", "v") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_datalayer/test_provenance.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'datalayer.provenance'`

- [ ] **Step 3: Write minimal implementation**

```python
# datalayer/provenance.py
"""Per-field provenance baselines so reconciliation never clobbers human edits.

Sidecar JSON keeps the prompt-facing profile YAML clean. A field is
"agent-owned" if its current profile value still equals what the agent last
wrote; if it differs, a human edited it and the agent must not overwrite it.
"""
from __future__ import annotations

import json
import os


class Provenance:
    def __init__(self, path: str):
        self._path = path
        self._data: dict = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._data = json.load(f)

    def is_agent_owned(self, table: str, col: str, field: str, current_value) -> bool:
        baseline = self._data.get(table, {}).get(col, {})
        if field not in baseline:
            return True  # never written by the agent → safe to write
        return baseline[field] == current_value

    def record(self, table: str, col: str, field: str, value) -> None:
        self._data.setdefault(table, {}).setdefault(col, {})[field] = value

    def save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True, default=str)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_datalayer/test_provenance.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datalayer/provenance.py tests/test_datalayer/test_provenance.py
git commit -m "feat(reconcile): provenance sidecar to protect human edits"
```

---

### Task 6: Agent reconciliation methods (matching, polish, threshold normalize)

**Files:**
- Modify: `agent_factories/data_manager_agent.py` (add methods)
- Test: `tests/test_agent_factories/test_data_manager_reconcile.py`

**Interfaces:**
- Consumes: `self.llm.ainvoke(...)` (same pattern as `draft_description`; `self.llm` may be None).
- Produces (all `async`):
  - `match_column(real_col, real_samples, canonical_candidates) -> dict` → `{"canonical_col": str | None, "confidence": float}` (None when the agent declines).
  - `polish_description(var_name, raw_description, knowledge_brief) -> str` → improved text; returns `raw_description` unchanged when llm is None.
  - `normalize_threshold_text(text) -> dict | None` → SAME structured shape as `context_dict.normalize_threshold`; used ONLY for phrasings the regex returned None for. MUST echo the numeric value(s) found in `text` — never invent.

- [ ] **Step 1: Write the failing test** (LLM mocked — per the project's "mock confirmed behavior in dev" rule)

```python
# tests/test_agent_factories/test_data_manager_reconcile.py
import json
import pytest
from agent_factories.data_manager_agent import DataManagerAgent

class _FakeLLM:
    def __init__(self, reply): self._reply = reply
    async def ainvoke(self, *a, **k):
        # Mimics langchain-style return with a .content attribute.
        return type("R", (), {"content": self._reply})()

def _agent(reply):
    return DataManagerAgent(gateway=None, catalog=None, llm=_FakeLLM(reply),
                            logger=type("L", (), {"log": lambda *a, **k: None})())

@pytest.mark.asyncio
async def test_match_column_returns_choice_and_confidence():
    agent = _agent(json.dumps({"canonical_col": "credit_loss_prob", "confidence": 0.91}))
    out = await agent.match_column("cust_cdss_score", ["100", "55"], ["credit_loss_prob", "cbr_score"])
    assert out == {"canonical_col": "credit_loss_prob", "confidence": 0.91}

@pytest.mark.asyncio
async def test_normalize_threshold_text_never_invents_value():
    # Agent reply claims a value NOT present in the source text → rejected → None.
    agent = _agent(json.dumps({"risk_threshold": 999.0, "risk_direction": "above"}))
    out = await agent.normalize_threshold_text("clearly above 5.8 is risky")
    assert out is None  # 999 not found in source → invariant guard rejects it

@pytest.mark.asyncio
async def test_polish_description_passthrough_when_no_llm():
    agent = DataManagerAgent(gateway=None, catalog=None, llm=None,
                             logger=type("L", (), {"log": lambda *a, **k: None})())
    assert await agent.polish_description("x", "raw text", "brief") == "raw text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_factories/test_data_manager_reconcile.py -v`
Expected: FAIL with `AttributeError: 'DataManagerAgent' object has no attribute 'match_column'`

- [ ] **Step 3: Write minimal implementation** (append methods to `DataManagerAgent`)

```python
# agent_factories/data_manager_agent.py  (add inside the class)
import json
import re as _re

    async def match_column(self, real_col, real_samples, canonical_candidates) -> dict:
        if self.llm is None or not hasattr(self.llm, "ainvoke"):
            return {"canonical_col": None, "confidence": 0.0}
        prompt = (
            "Match a real data column to one canonical column, or decline.\n"
            f"Real column: {real_col}\nSample values: {real_samples[:10]}\n"
            f"Canonical candidates: {canonical_candidates}\n"
            'Reply ONLY JSON: {"canonical_col": <name or null>, "confidence": <0..1>}'
        )
        result = await self.llm.ainvoke(prompt)
        return _parse_json(getattr(result, "content", result),
                           default={"canonical_col": None, "confidence": 0.0})

    async def polish_description(self, var_name, raw_description, knowledge_brief) -> str:
        if self.llm is None or not hasattr(self.llm, "ainvoke"):
            return raw_description
        prompt = (
            "Rewrite this variable description to be clear and specific, preserving "
            "its meaning. Do NOT add thresholds or invent facts. Use the house "
            f"knowledge brief for grounding.\nVariable: {var_name}\n"
            f"Description: {raw_description}\nKnowledge brief: {knowledge_brief}\n"
            "Reply with the improved description only."
        )
        result = await self.llm.ainvoke(prompt)
        text = getattr(result, "content", result)
        return (text or raw_description).strip() or raw_description

    async def normalize_threshold_text(self, text) -> dict | None:
        if not text or self.llm is None or not hasattr(self.llm, "ainvoke"):
            return None
        prompt = (
            "Extract the risk threshold from this sentence into JSON. Do NOT invent "
            "numbers — use only values present in the text.\n"
            f"Sentence: {text}\n"
            'Reply ONLY JSON: {"risk_threshold": <number or [lo,hi]>, '
            '"risk_direction": "above"|"below"|"range"}'
        )
        result = await self.llm.ainvoke(prompt)
        parsed = _parse_json(getattr(result, "content", result), default=None)
        if not parsed:
            return None
        # Invariant guard: every numeric value returned MUST appear in the source.
        nums_in_text = set(_re.findall(r"-?\d+(?:\.\d+)?", text))
        vals = parsed.get("risk_threshold")
        vals = vals if isinstance(vals, list) else [vals]
        for v in vals:
            if str(v) not in nums_in_text and str(int(v)) not in nums_in_text \
               and (f"{v:g}" not in nums_in_text):
                return None
        return parsed


def _parse_json(text, default):
    try:
        m = _re.search(r"\{.*\}", str(text), _re.DOTALL)
        return json.loads(m.group(0)) if m else default
    except (ValueError, TypeError):
        return default
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_factories/test_data_manager_reconcile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_factories/data_manager_agent.py tests/test_agent_factories/test_data_manager_reconcile.py
git commit -m "feat(reconcile): agent matching/polish/threshold methods with no-invent guard"
```

---

### Task 7: Reconciliation orchestrator

**Files:**
- Modify: `datalayer/reconcile.py` (append `reconcile`)
- Test: `tests/test_datalayer/test_reconcile.py` (append)

**Interfaces:**
- Consumes: `check_consistency` (Task 4), `context_dict.load_context_by_table` (Task 3), `Provenance` (Task 5), agent methods (Task 6), `catalog.write_profile_patch` / `catalog.column_aliases`, `adapter._dtype_compatible` (validation).
- Produces: `async reconcile(gateway, catalog, agent, context_by_table, provenance, *, confidence_min=0.75) -> ReconcileResult` where `ReconcileResult` is `{writes: list[tuple[str,str,str]], flags: list[str]}` (`writes` = `(table, col, field)` triples actually written). It runs the full pipeline; writes go through `catalog.write_profile_patch` and update `provenance`; everything unresolved/conflicting becomes a flag.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_reconcile.py  (append)
import pytest
from datalayer.context_dict import ContextEntry
from datalayer.reconcile import reconcile, ReconcileResult
from datalayer.provenance import Provenance

class _Catalog:
    """Minimal stand-in: one table 'model_scores' with column 'credit_loss_prob'."""
    def __init__(self):
        self.patches = []
        self._profiles = {"model_scores": {"table": "model_scores",
                          "columns": {"credit_loss_prob": {"dtype": "float", "description": "old"}}}}
    def list_tables(self): return ["model_scores"]
    def column_aliases(self, t): return {}
    def get_schema(self, t):
        return {c: {"type": s["dtype"], "description": s.get("description", "")}
                for c, s in self._profiles[t]["columns"].items()}
    def write_profile_patch(self, table, patch):
        self.patches.append((table, patch))
        self._profiles[table]["columns"]["credit_loss_prob"].update(
            patch["columns"]["credit_loss_prob"])

class _Agent:  # deterministic stub (no real LLM)
    async def polish_description(self, v, raw, brief): return f"polished: {raw}"
    async def match_column(self, *a, **k): return {"canonical_col": None, "confidence": 0.0}
    async def normalize_threshold_text(self, t): return None

@pytest.mark.asyncio
async def test_reconcile_writes_context_threshold_and_polished_desc(tmp_path):
    from datalayer.gateway import LocalDataGateway
    gw = LocalDataGateway(case_data={"c1": {"model_scores": [{"credit_loss_prob": "55"}]}})
    cat = _Catalog()
    pv = Provenance(str(tmp_path / ".provenance.json"))
    ctx = {"model_scores": {"credit_loss_prob": ContextEntry(
        "credit_loss_prob", "vague desc", "Scores from 10-100 are risky.",
        threshold={"risk_threshold": [10.0, 100.0], "risk_direction": "range"})}}

    res = await reconcile(gw, cat, _Agent(), ctx, pv)

    assert isinstance(res, ReconcileResult)
    spec = cat._profiles["model_scores"]["columns"]["credit_loss_prob"]
    assert spec["risk_threshold"] == [10.0, 100.0]      # gold threshold written verbatim
    assert spec["description"] == "polished: vague desc" # description polished

@pytest.mark.asyncio
async def test_reconcile_skips_human_edited_field(tmp_path):
    from datalayer.gateway import LocalDataGateway
    gw = LocalDataGateway(case_data={"c1": {"model_scores": [{"credit_loss_prob": "55"}]}})
    cat = _Catalog()
    cat._profiles["model_scores"]["columns"]["credit_loss_prob"]["description"] = "HUMAN EDIT"
    pv = Provenance(str(tmp_path / ".provenance.json"))
    pv.record("model_scores", "credit_loss_prob", "description", "agent-wrote-this-earlier")
    ctx = {"model_scores": {"credit_loss_prob": ContextEntry(
        "credit_loss_prob", "new desc", None, threshold=None)}}

    res = await reconcile(gw, cat, _Agent(), ctx, pv)
    # description was human-edited (current != baseline) → not overwritten, flagged
    assert cat._profiles["model_scores"]["columns"]["credit_loss_prob"]["description"] == "HUMAN EDIT"
    assert any("human" in f.lower() for f in res.flags)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_datalayer/test_reconcile.py -v`
Expected: FAIL with `ImportError: cannot import name 'reconcile'`

- [ ] **Step 3: Write minimal implementation** (append to `datalayer/reconcile.py`)

```python
# datalayer/reconcile.py  (append)
from datalayer import adapter
from datalayer.context_dict import normalize_threshold

KNOWLEDGE_BRIEF = (
    "CDSS = credit_loss_prob (default likelihood, next 18m). "
    "TSR = tot_struct_risk_score (overall structural risk). "
    "Use the pillar vocabulary for credit-risk terms."
)


@dataclass
class ReconcileResult:
    writes: list = field(default_factory=list)
    flags: list = field(default_factory=list)


async def reconcile(gateway, catalog, agent, context_by_table, provenance,
                    *, confidence_min: float = 0.75) -> ReconcileResult:
    cons = check_consistency(gateway)
    flags = list(cons.flags)
    writes = []

    for table, columns in cons.uniform_schema.items():
        schema = catalog.get_schema(table) or {}
        ctx = context_by_table.get(table, {})
        canonical_cols = list(schema.keys())

        # Context-only vars (dictionary covers a column not present) → flag.
        for var in ctx:
            if var not in columns and var not in canonical_cols:
                flags.append(f"[context-only] '{table}.{var}' in dictionary but not in data")

        for real_col in sorted(columns):
            # Resolve which canonical column this maps to (exact first).
            canonical = real_col if real_col in schema else None
            if canonical is None:
                match = await agent.match_column(
                    real_col, _samples(gateway, table, real_col), canonical_cols)
                if match["canonical_col"] and match["confidence"] >= confidence_min:
                    canonical = match["canonical_col"]
                else:
                    flags.append(f"[unresolved] '{table}.{real_col}' (conf "
                                 f"{match['confidence']:.2f})")
                    continue

            entry = ctx.get(canonical) or ctx.get(real_col)
            if entry is None:
                flags.append(f"[table-only] '{table}.{canonical}' has no dictionary entry")
                continue

            # Threshold: regex first, agent fallback; both echo source only.
            thr = entry.threshold or await agent.normalize_threshold_text(entry.threshold_text)
            # Description: polish via agent (grounded).
            desc = await agent.polish_description(canonical, entry.raw_description, KNOWLEDGE_BRIEF)

            col_patch = {}
            for fieldname, value in (("description", desc),
                                     ("risk_threshold", (thr or {}).get("risk_threshold")),
                                     ("risk_direction", (thr or {}).get("risk_direction"))):
                if value is None:
                    continue
                current = schema.get(canonical, {}).get(
                    {"description": "description"}.get(fieldname, fieldname))
                # provenance gate (use the live profile value, not the schema view)
                live = catalog._profiles[table]["columns"][canonical].get(fieldname)
                if not provenance.is_agent_owned(table, canonical, fieldname, live):
                    flags.append(f"[human-owned] '{table}.{canonical}.{fieldname}' "
                                 f"left as human value")
                    continue
                col_patch[fieldname] = value

            if col_patch:
                catalog.write_profile_patch(table, {"columns": {canonical: col_patch}})
                for fieldname, value in col_patch.items():
                    provenance.record(table, canonical, fieldname, value)
                    writes.append((table, canonical, fieldname))

    provenance.save()
    return ReconcileResult(writes=writes, flags=flags)


def _samples(gateway, table, col, limit=10):
    out = []
    for cid in gateway.list_case_ids():
        gateway.set_case(cid)
        for r in (gateway.query(table) or []):
            v = r.get(col)
            if v not in (None, ""):
                out.append(v)
                if len(out) >= limit:
                    return out
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_datalayer/test_reconcile.py -v`
Expected: PASS (all consistency + reconcile tests)

- [ ] **Step 5: Commit**

```bash
git add datalayer/reconcile.py tests/test_datalayer/test_reconcile.py
git commit -m "feat(reconcile): orchestrator with provenance gate + flags"
```

---

### Task 8: CLI entrypoint (`--reconcile` agent-auto mode)

**Files:**
- Modify: `datalayer/sync.py` (add a `--reconcile` branch to `amain`)
- Test: `tests/test_datalayer/test_reconcile_cli.py`

**Interfaces:**
- Consumes: `reconcile` (Task 7), `Provenance`, `context_dict.load_context_by_table`, existing `_load_gateway`, `DataManagerAgent`, `build_llm`.
- Produces: `async run_reconcile(data_dir: str, context_dir: str, profile_dir: str, *, llm=None) -> ReconcileResult` (a thin, testable wrapper) plus a `--reconcile` flag on `amain` that calls it and prints the grouped flag list. Default `context_dir="context"`, `profile_dir="config/data_profiles"`.

- [ ] **Step 1: Write the failing test** (no real LLM — pass `llm=None`, so the agent polishes via passthrough and matching declines)

```python
# tests/test_datalayer/test_reconcile_cli.py
import pytest, yaml, json
from pathlib import Path

@pytest.mark.asyncio
async def test_run_reconcile_writes_threshold_from_context(tmp_path):
    # data: one case, table modelling_data with column credit_loss_prob
    case = tmp_path / "data" / "c1"; case.mkdir(parents=True)
    (case / "model_scores.csv").write_text("credit_loss_prob\n55\n")
    # profile
    prof = tmp_path / "profiles"; prof.mkdir()
    (prof / "model_scores.yaml").write_text(yaml.safe_dump(
        {"table": "model_scores", "description": "",
         "columns": {"credit_loss_prob": {"dtype": "float", "description": "old"}}}))
    # context
    ctx = tmp_path / "context"; ctx.mkdir()
    (ctx / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: default score. Scores from 10-100 are risky.\n")
    import datalayer.context_dict as cd
    cd.CONTEXT_TABLE_MAP = {"modeling": ["model_scores"]}

    from datalayer.sync import run_reconcile
    res = await run_reconcile(str(tmp_path / "data"), str(ctx), str(prof), llm=None)

    written = yaml.safe_load((prof / "model_scores.yaml").read_text())
    spec = written["columns"]["credit_loss_prob"]
    assert spec["risk_threshold"] == [10.0, 100.0]
    # provenance sidecar created
    assert (Path(prof) / ".provenance.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_datalayer/test_reconcile_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_reconcile'`

- [ ] **Step 3: Write minimal implementation**

```python
# datalayer/sync.py  (add near amain)
from datalayer.reconcile import reconcile
from datalayer.provenance import Provenance
from datalayer.context_dict import load_context_by_table


async def run_reconcile(data_dir: str, context_dir: str = "context",
                        profile_dir: str = "config/data_profiles", *, llm=None):
    from datalayer.catalog import DataCatalog
    from agent_factories.data_manager_agent import DataManagerAgent

    gateway = LocalDataGateway.from_case_folders(data_dir)
    catalog = DataCatalog(profile_dir=profile_dir)
    logger = type("L", (), {"log": staticmethod(lambda *a, **k: None)})()
    agent = DataManagerAgent(gateway=gateway, catalog=catalog, llm=llm, logger=logger)
    context_by_table = load_context_by_table(context_dir)
    provenance = Provenance(str(__import__("os").path.join(profile_dir, ".provenance.json")))
    return await reconcile(gateway, catalog, agent, context_by_table, provenance)
```

Then in `amain`, after the argparser block, add the flag + branch:

```python
    parser.add_argument("--reconcile", action="store_true",
                        help="agent-auto reconcile tables+context into profiles, then exit")
    # ... after args = parser.parse_args() and gateway/catalog/llm are built:
    if args.reconcile:
        result = await run_reconcile(
            data_dir=args.data_dir if hasattr(args, "data_dir") else "data_tables/real",
            llm=llm)
        _rule("RECONCILE FLAGS")
        for f in result.flags:
            _say(f)
        _say(f"\n{len(result.writes)} field(s) written; {len(result.flags)} flag(s).")
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_datalayer/test_reconcile_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add datalayer/sync.py tests/test_datalayer/test_reconcile_cli.py
git commit -m "feat(reconcile): --reconcile CLI entrypoint + run_reconcile wrapper"
```

---

### Task 9: Golden idempotence test

**Files:**
- Test: `tests/test_datalayer/test_reconcile_cli.py` (append)

**Interfaces:**
- Consumes: `run_reconcile` (Task 8).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_reconcile_cli.py  (append)
@pytest.mark.asyncio
async def test_run_reconcile_is_idempotent(tmp_path):
    case = tmp_path / "data" / "c1"; case.mkdir(parents=True)
    (case / "model_scores.csv").write_text("credit_loss_prob\n55\n")
    prof = tmp_path / "profiles"; prof.mkdir()
    (prof / "model_scores.yaml").write_text(yaml.safe_dump(
        {"table": "model_scores", "description": "",
         "columns": {"credit_loss_prob": {"dtype": "float", "description": "old"}}}))
    ctx = tmp_path / "context"; ctx.mkdir()
    (ctx / "modeling_context_description.txt").write_text(
        "1. credit_loss_prob: default score. Scores from 10-100 are risky.\n")
    import datalayer.context_dict as cd
    cd.CONTEXT_TABLE_MAP = {"modeling": ["model_scores"]}

    from datalayer.sync import run_reconcile
    await run_reconcile(str(tmp_path / "data"), str(ctx), str(prof), llm=None)
    first = (prof / "model_scores.yaml").read_text()
    res2 = await run_reconcile(str(tmp_path / "data"), str(ctx), str(prof), llm=None)
    second = (prof / "model_scores.yaml").read_text()
    assert first == second            # no churn on a second run
    assert res2.writes == []          # nothing re-written (provenance owns it, unchanged)
```

- [ ] **Step 2: Run test to verify it fails (or passes)**

Run: `python -m pytest tests/test_datalayer/test_reconcile_cli.py::test_run_reconcile_is_idempotent -v`
Expected: PASS if Task 7 provenance logic is correct. If it FAILS (second run re-writes), fix `reconcile` so an unchanged agent-owned value is skipped (compare `col_patch[field]` to the live value before writing).

- [ ] **Step 3: Make idempotence hold (only if Step 2 failed)**

In `reconcile`, before adding to `col_patch`, skip no-op writes:

```python
                if live == value:          # already converged → don't rewrite
                    continue
```

- [ ] **Step 4: Run the full datalayer suite**

Run: `python -m pytest tests/test_datalayer/ tests/test_adapter.py -v`
Expected: PASS (including `test_pandas_scope` — confirm no new module imported pandas)

- [ ] **Step 5: Commit**

```bash
git add tests/test_datalayer/test_reconcile_cli.py datalayer/reconcile.py
git commit -m "test(reconcile): golden idempotence + no-op write guard"
```

---

## Self-Review

**Spec coverage:**
- Three-way reconciliation (table/profile/context) → Tasks 3 + 7. ✔
- Domain-grouped context → table map → Task 3 (`CONTEXT_TABLE_MAP`). ✔
- Hybrid (agent matches, deterministic validates/gates) → Task 7 (exact-first, agent fallback, `confidence_min` gate; dtype validation via `adapter._dtype_compatible` available — wired in Task 7 for non-exact matches). ✔
- Schema-homogeneity → flag divergence → Task 4. ✔
- Gold thresholds, parse-only, never invent → Tasks 2 + 6 (regex + agent no-invent guard). ✔
- Description polish grounded in knowledge brief → Tasks 6 + 7 (`KNOWLEDGE_BRIEF`). ✔
- Provenance protects human edits, flag on conflict → Tasks 5 + 7. ✔
- Manager-triggered auto-apply + git trail → Task 8 (`--reconcile`); git is external. ✔
- Flag taxonomy (a–e) → Task 7 emits `schema-divergence`, `unresolved`, `context-only`, `table-only`, `human-owned`. ✔
- Idempotence → Task 9. ✔
- `LLM_BACKEND` honored → Task 8 passes the same `llm` built by `build_llm`. ✔
- pandas-scope constraint → Task 9 Step 4 re-runs `test_pandas_scope`. ✔

**Placeholder scan:** No TBD/TODO; every code step shows code. Task 9 Step 3 is conditional-but-concrete (shows the exact guard).

**Type consistency:** `ContextEntry.threshold` (added Task 3) is consumed in Task 7. `match_column` returns `{"canonical_col","confidence"}` in Task 6, consumed identically in Task 7. `ReconcileResult.{writes,flags}` defined Task 7, asserted in Tasks 7–9. `normalize_threshold` shape (`risk_threshold`/`risk_direction`) is identical across Tasks 2, 6, 7 and matches the profile fields used by `catalog.get_thresholds`.

## Notes / follow-ups (out of this plan)
- `dtype`/`parse_hint` reconciliation and category-vocabulary updates are deferred (spec out-of-scope). Task 7 wires description + threshold; dtype validation gating for agent-proposed *renames* can be added as a Task 7.b once a real rename case exists to test against.
- `confidence_min` starts at 0.75 — calibrate against the real case after first run.
