# Subsystem Iteration Notebooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship four self-contained Jupyter notebooks under `notebooks/` — one per subsystem (team construction, report agent, compare/review, data query) — each with a fixed 6-cell skeleton, committed fixtures under `notebooks/fixtures/<subsystem>/`, and committed output cells as the regression signal.

**Architecture:** Each notebook builds the real runtime (logger, firewall, llm, gateway, catalog, registry) inline, loads a JSON fixture, calls exactly one subsystem entry point, and renders the result with inline helpers. No shared harness module; duplication across the four notebooks is accepted in exchange for self-containment.

**Tech Stack:** Jupyter (`nbformat`/VSCode Jupyter), Python 3.11, `nest_asyncio`, `python-dotenv`, existing project modules (`orchestrator.orchestrator.Orchestrator`, `agents.report_agent.ReportAgent`, `agents.general_specialist.GeneralSpecialist`, `agents.session_registry.SessionRegistry`, `agents.base_agent.BaseSpecialistAgent`, `data.gateway.SimulatedDataGateway`, `data.generator.DataGenerator`, `data.catalog.DataCatalog`, `gateway.firewall_stack.FirewallStack`, `gateway.llm_factory.build_llm`, `logger.event_logger.EventLogger`, `config.pillar_loader.PillarLoader`, `tools.data_tools.init_tools`, `skills.domain.loader.load_domain_skill`/`list_domain_skills`).

**Spec reference:** `docs/specs/2026-04-24-subsystem-iteration-notebooks-design.md`.

---

## Pre-flight notes for the implementer

- The project has existing notebooks `notebooks/test_chat_mode.ipynb`, `test_data_access.ipynb`, `test_report_mode.ipynb` — **leave them untouched**. Their constructor signatures are partly stale (e.g., `FirewallStack(adapter=...)`); follow the signatures in `main.py`, not those notebooks.
- Current runtime entry (from `main.py`): build `FirewallStack(logger=logger)`, then `llm = build_llm(model_name, firewall)`. Pass `llm` (a `FirewalledModel`) to `Orchestrator`, `ReportAgent`, `GeneralSpecialist`, and `registry.get_or_create`.
- `Orchestrator.plan_team`, `ReportAgent.run`, `GeneralSpecialist.compare`, and `BaseSpecialistAgent.run` are all `async` — use `await` inside cells. `nest_asyncio.apply()` is required to await at top level in Jupyter.
- `data_tables/` is empty in the repo (only has a `README.md`); `SimulatedDataGateway.from_case_folders(...)` returns an empty gateway, so the notebooks must fall back to the `DataGenerator` path (same fallback as `main.py`).
- `reports/` is empty in the repo; the Report Agent's default coverage will be `"none"` until someone stages curated markdown. That's fine for the iteration notebook — exercising the empty-folder path is a legitimate polish use case. Include a short markdown cell that tells the user how to stage a report folder when they want to test coverage=full.
- Commit each notebook with its output cells populated (the notebook's own "Run All" result becomes the committed artifact). Don't commit logs from `logs/`; the runtime creates that folder automatically.

### Notebook creation mechanics

There are two reasonable ways to create each notebook:

1. **VSCode / JupyterLab UI (simpler):** File → New → Jupyter Notebook, then paste each cell's content in order. Cell type (code vs markdown) is noted on every cell below.
2. **`nbformat` script:** write a tiny Python script that assembles the cells programmatically. Only use this if you find yourself re-creating the notebooks several times; the plan assumes option 1.

In either case, set the notebook's kernel to the project's Python (the one where `pip install -r requirements.txt` ran).

---

## Task 1: Scaffold fixture directories and initial team_construction fixture

**Files:**
- Create: `notebooks/fixtures/team_construction/basic_case.json`
- Create: `notebooks/fixtures/report_agent/basic_case.json`
- Create: `notebooks/fixtures/compare_review/basic_case.json`
- Create: `notebooks/fixtures/data_query/basic_case.json`
- Create: `notebooks/fixtures/.gitkeep` (optional; skip if all four subdirs already have JSONs committed)

- [ ] **Step 1: Create the four fixture directories and commit `basic_case.json` files**

Each subsystem gets its own fixture directory. Create all four at once — even though only `team_construction/basic_case.json` is used in Task 2, landing the shape for all four avoids a later "where does this go?" question during replication tasks.

Contents:

`notebooks/fixtures/team_construction/basic_case.json`:

```json
{
  "question": "Is this customer's credit risk acceptable?",
  "pillar": "credit_risk",
  "available_specialists": null,
  "active_specialists": [],
  "case_id": "CASE-00001",
  "notes": "Baseline multi-specialist selection case. available_specialists=null -> all domains from skills/domain/."
}
```

`notebooks/fixtures/report_agent/basic_case.json`:

```json
{
  "question": "What are the main credit risk findings for this customer?",
  "case_id": "CASE-00001",
  "notes": "With reports/CASE-00001/ empty, coverage will be 'none'. To exercise coverage=full, drop curated markdown into reports/CASE-00001/ before re-running."
}
```

`notebooks/fixtures/compare_review/basic_case.json`:

```json
{
  "question": "Is this customer's credit risk acceptable?",
  "specialist_outputs": {
    "bureau": {
      "domain": "bureau",
      "question": "What do the bureau scores tell us about credit risk?",
      "mode": "chat",
      "findings": "Bureau scores are stable and above internal thresholds (FICO 720, SBFE 680). No recent deterioration.",
      "evidence": ["FICO 720 (latest)", "SBFE 680 (latest)"],
      "implications": ["External credit profile is solid"],
      "data_gaps": [],
      "raw_data": {}
    },
    "spend_payments": {
      "domain": "spend_payments",
      "question": "How have spend and payment patterns trended recently?",
      "mode": "chat",
      "findings": "Payment behaviour shows two returned payments in the last 60 days; spend velocity flat.",
      "evidence": ["2 returned payments 2025-10 to 2025-12", "monthly spend ~$8k/mo flat"],
      "implications": ["Early warning signal on payment reliability"],
      "data_gaps": [],
      "raw_data": {}
    }
  },
  "notes": "Two hand-authored specialist outputs that disagree in tone (bureau calm vs spend_payments concerned). Exercises the Compare skill's contradiction path. Regenerate via the REGENERATE=True cell to replace with a live specialist run."
}
```

`notebooks/fixtures/data_query/basic_case.json`:

```json
{
  "question": "Summarise this customer's recent spend and payment behaviour.",
  "pillar": "credit_risk",
  "case_id": "CASE-00001",
  "specialist": "spend_payments",
  "notes": "Polishes the spend_payments specialist's tool-calling loop. Switch `specialist` to any other domain under skills/domain/ to polish that domain instead."
}
```

- [ ] **Step 2: Commit**

```bash
git add notebooks/fixtures/
git commit -m "feat(notebooks): scaffold fixture dirs for subsystem iteration notebooks"
```

---

## Task 2: Build pilot notebook `test_team_construction.ipynb`

**Files:**
- Create: `notebooks/test_team_construction.ipynb`

The notebook follows the 6-cell skeleton from spec §2 exactly. Paste each cell in order, setting cell type (markdown vs code) as indicated.

- [ ] **Step 1: Create the notebook file**

In VSCode: right-click on `notebooks/` → New File → `test_team_construction.ipynb`. Ensure the Python kernel is set to the project's interpreter.

- [ ] **Step 2: Cell 1 (markdown) — header**

Cell type: **Markdown**

```markdown
# Team Construction — Iteration Notebook

Polish how the orchestrator picks specialists and decomposes sub-questions.

**Edit surfaces:**
- Non-engineer: `skills/workflow/team_construction.md`, `config/pillars/<pillar>.yaml`, `skills/domain/*.md`.
- Engineer: `orchestrator/orchestrator.py` (see `_select_team`, `_split_sub_questions`).

**Workflow:** edit a file above → Run All → read Cell 5's rendered plan → repeat. Cell 6's raw JSON dump is the regression signal — commit the notebook with output cells populated so future diffs are meaningful.
```

- [ ] **Step 3: Cell 2 (code) — knobs + imports**

Cell type: **Code**

```python
# ═══════════════ KNOBS ═══════════════
FIXTURE = "basic_case"
REGENERATE = False          # True = write current values back to fixtures/team_construction/<FIXTURE>.json
MODEL = "gpt-4.1"
# ═════════════════════════════════════

import json
import os
import sys
from pathlib import Path

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
load_dotenv()

# Repo root on sys.path so the project modules import cleanly.
PROJECT_ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from gateway.llm_factory import build_llm
from logger.event_logger import EventLogger
from orchestrator.orchestrator import Orchestrator
from skills.domain.loader import list_domain_skills
from tools.data_tools import init_tools

FIXTURE_PATH = PROJECT_ROOT / "notebooks" / "fixtures" / "team_construction" / f"{FIXTURE}.json"
print(f"Fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
print(f"Regenerate: {REGENERATE}")
```

- [ ] **Step 4: Cell 3 (code) — environment + gateway + catalog**

Cell type: **Code**

```python
# Logger + firewall + LLM
logger = EventLogger(session_id="polish-team-construction")
firewall = FirewallStack(logger=logger)
llm = build_llm(MODEL, firewall)

# Load the pillar YAML BEFORE we know which pillar we want, so we can re-use
# PillarLoader after the fixture is loaded in Cell 4.
pillar_loader = PillarLoader(pillar_dir=str(PROJECT_ROOT / "config" / "pillars"))

# Gateway: CSV-first, generator fallback (same logic as main.py).
_DATA_TABLES_DIR = PROJECT_ROOT / "data_tables"
csv_gateway = SimulatedDataGateway.from_case_folders(str(_DATA_TABLES_DIR))
if csv_gateway.list_case_ids():
    gateway = csv_gateway
    print(f"Data source: csv ({len(csv_gateway.list_case_ids())} cases)")
else:
    gen = DataGenerator(
        profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"),
        seed=42, cases=50,
    )
    gen.load_profiles()
    tables_raw = gen.generate_all()
    gateway = SimulatedDataGateway.from_generated(tables_raw)
    print(f"Data source: generator ({len(gateway.list_case_ids())} cases)")

catalog = DataCatalog(profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"))
init_tools(gateway, catalog, logger=logger)

registry = SessionRegistry()
print(f"Available case IDs (first 5): {gateway.list_case_ids()[:5]}")
```

- [ ] **Step 5: Cell 4 (code) — fixture load or regenerate**

Cell type: **Code**

```python
if REGENERATE:
    current = {
        "question": "Is this customer's credit risk acceptable?",
        "pillar": "credit_risk",
        "available_specialists": None,
        "active_specialists": [],
        "case_id": gateway.list_case_ids()[0],
        "notes": f"Regenerated from case {gateway.list_case_ids()[0]}.",
    }
    FIXTURE_PATH.write_text(json.dumps(current, indent=2) + "\n")
    fixture = current
    print(f"Wrote fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
else:
    fixture = json.loads(FIXTURE_PATH.read_text())
    print(f"Loaded fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")

# Pick a usable case_id: the fixture's preferred value, or the first available if it's missing.
available = gateway.list_case_ids()
case_id = fixture["case_id"] if fixture["case_id"] in available else available[0]
if case_id != fixture["case_id"]:
    print(f"  (fixture case '{fixture['case_id']}' not available; using '{case_id}')")
gateway.set_case(case_id)

pillar_yaml = pillar_loader.load(fixture["pillar"]) or {}
print(f"Pillar: {fixture['pillar']} | Case: {case_id}")
print(f"Question: {fixture['question']}")
```
```

- [ ] **Step 6: Cell 5 (code) — call plan_team + render**

Cell type: **Code**

```python
from IPython.display import Markdown, display
from skills.domain.loader import load_domain_skill

orchestrator = Orchestrator(
    llm, logger, registry, fixture["pillar"],
    pillar_config=pillar_yaml, catalog=catalog,
)

available_specialists = fixture["available_specialists"] or list_domain_skills()
active_specialists = fixture["active_specialists"]

plan = await orchestrator.plan_team(
    question=fixture["question"],
    available_specialists=available_specialists,
    active_specialists=active_specialists,
)

# Render
selected = [p.specialist for p in plan]
not_picked = [s for s in available_specialists if s not in selected]

lines = [f"### Root question\n\n{fixture['question']}\n"]
lines.append(f"**Pillar:** `{fixture['pillar']}` — **Case:** `{case_id}`\n")
lines.append(f"**Available specialists:** {', '.join(available_specialists)}\n")
lines.append(f"**Selected ({len(selected)}):**")
for a in plan:
    skill = load_domain_skill(a.specialist)
    tables = ", ".join(skill.data_hints) if skill and skill.data_hints else "(no tables)"
    lines.append(f"- **{a.specialist}** — tables: {tables}")
    lines.append(f"  - sub-question: _{a.sub_question}_")
if not_picked:
    lines.append(f"\n**Not picked:** {', '.join(not_picked)}")
display(Markdown("\n".join(lines)))
```

- [ ] **Step 7: Cell 6 (code) — raw JSON dump (regression signal)**

Cell type: **Code**

```python
# This cell's output is the regression signal. Commit the notebook with this
# output populated; diff against future runs to see what an edit changed.
print(json.dumps([p.model_dump() for p in plan], indent=2))
```

- [ ] **Step 8: Run the notebook top-to-bottom**

From VSCode's notebook UI, click "Run All". Expected:
- Cell 2 prints the fixture path and `Regenerate: False`.
- Cell 3 prints "Data source: generator (50 cases)" and a list of case IDs.
- Cell 4 prints the chosen pillar and case.
- Cell 5 renders a markdown block with selected specialists and their sub-questions.
- Cell 6 prints a JSON array of `{specialist, sub_question}` entries.

If any cell errors, fix the issue before proceeding. Common failure modes:
- `OPENAI_API_KEY` not set — ensure `.env` at repo root has the key.
- `nest_asyncio` not installed — `pip install nest_asyncio` (should already be in `requirements.txt`).
- `list_domain_skills()` returns empty — verify `skills/domain/*.md` files exist.

- [ ] **Step 9: Commit the notebook with output cells populated**

```bash
git add notebooks/test_team_construction.ipynb
git commit -m "feat(notebooks): add team_construction iteration notebook (pilot)"
```

---

## Task 3: Build `test_report_agent.ipynb`

**Files:**
- Create: `notebooks/test_report_agent.ipynb`

Same 6-cell skeleton as Task 2. Cells 1-4 are near-identical to the pilot; Cells 5 and 6 swap in `ReportAgent.run`. No shared imports extracted.

- [ ] **Step 1: Create the notebook file**

In VSCode: right-click on `notebooks/` → New File → `test_report_agent.ipynb`.

- [ ] **Step 2: Cell 1 (markdown) — header**

Cell type: **Markdown**

```markdown
# Report Agent — Iteration Notebook

Polish how curated-report ingestion decides coverage and extracts evidence.

**Edit surfaces:**
- Non-engineer: `skills/workflow/report_needle.md`, `skills/workflow/report_analysis.md`, the curated `reports/<case_id>/*.md` themselves.
- Engineer: `agents/report_agent.py`.

**Note:** if `reports/<case_id>/` is empty, coverage will be `"none"` and Step 2 is skipped. To test `coverage=full`, stage one or more `.md` files under `reports/<case_id>/` before running.

**Workflow:** edit a file above → Run All → read Cell 5's rendered draft → repeat. Cell 6's raw JSON is the regression signal.
```

- [ ] **Step 3: Cell 2 (code) — knobs + imports**

Cell type: **Code**

```python
# ═══════════════ KNOBS ═══════════════
FIXTURE = "basic_case"
REGENERATE = False
MODEL = "gpt-4.1"
# ═════════════════════════════════════

import json
import os
import sys
from pathlib import Path

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.report_agent import ReportAgent
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from gateway.llm_factory import build_llm
from logger.event_logger import EventLogger
from tools.data_tools import init_tools

FIXTURE_PATH = PROJECT_ROOT / "notebooks" / "fixtures" / "report_agent" / f"{FIXTURE}.json"
REPORTS_DIR = PROJECT_ROOT / "reports"
print(f"Fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
```

- [ ] **Step 4: Cell 3 (code) — environment + gateway (for `init_tools`)**

Cell type: **Code**

Note: `ReportAgent.run` itself does not need the gateway — the gateway lives here only because `init_tools` expects one and keeps the environment shape parallel to Task 2's pilot.

```python
logger = EventLogger(session_id="polish-report-agent")
firewall = FirewallStack(logger=logger)
llm = build_llm(MODEL, firewall)

_DATA_TABLES_DIR = PROJECT_ROOT / "data_tables"
csv_gateway = SimulatedDataGateway.from_case_folders(str(_DATA_TABLES_DIR))
if csv_gateway.list_case_ids():
    gateway = csv_gateway
else:
    gen = DataGenerator(
        profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"),
        seed=42, cases=50,
    )
    gen.load_profiles()
    tables_raw = gen.generate_all()
    gateway = SimulatedDataGateway.from_generated(tables_raw)

catalog = DataCatalog(profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"))
init_tools(gateway, catalog, logger=logger)
print(f"Available case IDs (first 5): {gateway.list_case_ids()[:5]}")
```

- [ ] **Step 5: Cell 4 (code) — fixture load or regenerate**

Cell type: **Code**

```python
if REGENERATE:
    current = {
        "question": "What are the main credit risk findings for this customer?",
        "case_id": gateway.list_case_ids()[0],
        "notes": f"Regenerated from case {gateway.list_case_ids()[0]}.",
    }
    FIXTURE_PATH.write_text(json.dumps(current, indent=2) + "\n")
    fixture = current
    print(f"Wrote fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
else:
    fixture = json.loads(FIXTURE_PATH.read_text())
    print(f"Loaded fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")

case_folder = REPORTS_DIR / fixture["case_id"]
print(f"Case folder: {case_folder.relative_to(PROJECT_ROOT)}")
print(f"Exists: {case_folder.exists()} | Files: {sorted(p.name for p in case_folder.glob('*.md')) if case_folder.exists() else '[]'}")
print(f"Question: {fixture['question']}")
```

- [ ] **Step 6: Cell 5 (code) — call ReportAgent.run + render**

Cell type: **Code**

```python
from IPython.display import Markdown, display

report_agent = ReportAgent(llm, logger)
draft = await report_agent.run(fixture["question"], case_folder)

lines = [f"### Question\n\n{fixture['question']}\n"]
lines.append(f"**Coverage:** `{draft.coverage}`  |  **Files consulted:** {', '.join(draft.files_consulted) if draft.files_consulted else '(none)'}\n")
lines.append(f"**Answer:**\n\n{draft.answer or '_(empty — coverage=none or analysis blocked)_'}\n")
if draft.evidence_excerpts:
    lines.append("**Evidence excerpts:**")
    for i, ex in enumerate(draft.evidence_excerpts, 1):
        lines.append(f"{i}. {ex}")
display(Markdown("\n".join(lines)))
```

- [ ] **Step 7: Cell 6 (code) — raw JSON dump**

Cell type: **Code**

```python
print(json.dumps(draft.model_dump(), indent=2))
```

- [ ] **Step 8: Run the notebook top-to-bottom**

"Run All" and verify:
- Cell 4 prints `Exists: False | Files: []` (expected — `reports/` is empty in the repo).
- Cell 5 renders `Coverage: none` with empty answer.
- Cell 6 prints a JSON object with `coverage: "none"`.

This validates the empty-folder path. To exercise coverage=full, stage a real markdown file under `reports/<case_id>/` and re-run; no code change needed.

- [ ] **Step 9: Commit**

```bash
git add notebooks/test_report_agent.ipynb
git commit -m "feat(notebooks): add report_agent iteration notebook"
```

---

## Task 4: Build `test_compare_review.ipynb`

**Files:**
- Create: `notebooks/test_compare_review.ipynb`

This notebook's Cell 4 is the one that diverges most from the pilot: the `REGENERATE=True` branch runs a live partial team pipeline to snapshot real `SpecialistOutput` values into the fixture. The default path (`REGENERATE=False`) deserialises the hand-authored fixture JSON into `SpecialistOutput` models and never touches the LLM until Cell 5.

- [ ] **Step 1: Create the notebook file**

VSCode → New File → `test_compare_review.ipynb`.

- [ ] **Step 2: Cell 1 (markdown) — header**

Cell type: **Markdown**

```markdown
# Compare / Review — Iteration Notebook

Polish how the General Specialist detects contradictions and cross-domain insights.

**Edit surfaces:**
- Non-engineer: `skills/workflow/comparison.md`.
- Engineer: `agents/general_specialist.py`.

**Regeneration:** set `REGENERATE=True` and Cell 4 runs a live team dispatch to capture fresh `specialist_outputs` from the configured case; otherwise it reads the hand-authored fixture JSON.

**Workflow:** edit a file above → Run All → read Cell 5's rendered ReviewReport → repeat. Cell 6's raw JSON is the regression signal.
```

- [ ] **Step 3: Cell 2 (code) — knobs + imports**

Cell type: **Code**

```python
# ═══════════════ KNOBS ═══════════════
FIXTURE = "basic_case"
REGENERATE = False          # True = run live team dispatch and snapshot specialist_outputs
MODEL = "gpt-4.1"
# ═════════════════════════════════════

import json
import os
import sys
from pathlib import Path

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.general_specialist import GeneralSpecialist
from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from gateway.llm_factory import build_llm
from logger.event_logger import EventLogger
from models.types import SpecialistOutput
from orchestrator.orchestrator import Orchestrator
from skills.domain.loader import list_domain_skills, load_domain_skill
from tools.data_tools import init_tools

FIXTURE_PATH = PROJECT_ROOT / "notebooks" / "fixtures" / "compare_review" / f"{FIXTURE}.json"
print(f"Fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
```

- [ ] **Step 4: Cell 3 (code) — environment + gateway + catalog**

Cell type: **Code**

```python
logger = EventLogger(session_id="polish-compare-review")
firewall = FirewallStack(logger=logger)
llm = build_llm(MODEL, firewall)

_DATA_TABLES_DIR = PROJECT_ROOT / "data_tables"
csv_gateway = SimulatedDataGateway.from_case_folders(str(_DATA_TABLES_DIR))
if csv_gateway.list_case_ids():
    gateway = csv_gateway
else:
    gen = DataGenerator(
        profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"),
        seed=42, cases=50,
    )
    gen.load_profiles()
    tables_raw = gen.generate_all()
    gateway = SimulatedDataGateway.from_generated(tables_raw)

catalog = DataCatalog(profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"))
init_tools(gateway, catalog, logger=logger)
registry = SessionRegistry()
pillar_loader = PillarLoader(pillar_dir=str(PROJECT_ROOT / "config" / "pillars"))
print(f"Available case IDs (first 5): {gateway.list_case_ids()[:5]}")
```

- [ ] **Step 5: Cell 4 (code) — fixture load or regenerate (live partial team run)**

Cell type: **Code**

```python
if REGENERATE:
    # Live partial team run: dispatch each specialist against a real case and
    # snapshot their outputs. Uses the orchestrator's own planning path so
    # the captured outputs match what the full pipeline would produce.
    REGEN_PILLAR = "credit_risk"
    REGEN_CASE = gateway.list_case_ids()[0]
    REGEN_QUESTION = "Is this customer's credit risk acceptable?"

    gateway.set_case(REGEN_CASE)
    pillar_yaml = pillar_loader.load(REGEN_PILLAR) or {}
    orchestrator = Orchestrator(
        llm, logger, registry, REGEN_PILLAR,
        pillar_config=pillar_yaml, catalog=catalog,
    )
    plan = await orchestrator.plan_team(
        question=REGEN_QUESTION,
        available_specialists=list_domain_skills(),
        active_specialists=[],
    )

    import asyncio
    async def _dispatch(assignment):
        skill = load_domain_skill(assignment.specialist)
        if skill is None:
            return None
        agent = registry.get_or_create(
            domain=assignment.specialist,
            pillar=REGEN_PILLAR,
            domain_skill=skill,
            pillar_yaml=pillar_yaml,
            llm=llm,
            logger=logger,
        )
        out = await agent.run(assignment.sub_question, mode="chat", root_question=REGEN_QUESTION)
        return assignment.specialist, out

    pairs = await asyncio.gather(*(_dispatch(a) for a in plan))
    specialist_outputs = {name: out for pair in pairs if pair is not None for name, out in [pair]}

    current = {
        "question": REGEN_QUESTION,
        "specialist_outputs": {d: o.model_dump() for d, o in specialist_outputs.items()},
        "notes": f"Regenerated from case {REGEN_CASE}, pillar {REGEN_PILLAR}.",
    }
    FIXTURE_PATH.write_text(json.dumps(current, indent=2) + "\n")
    fixture = current
    print(f"Wrote fixture with {len(specialist_outputs)} specialist outputs: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
else:
    fixture = json.loads(FIXTURE_PATH.read_text())
    specialist_outputs = {
        domain: SpecialistOutput(**payload)
        for domain, payload in fixture["specialist_outputs"].items()
    }
    print(f"Loaded fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")

print(f"Question: {fixture['question']}")
print(f"Specialist outputs: {list(specialist_outputs.keys())}")
```

- [ ] **Step 6: Cell 5 (code) — call GeneralSpecialist.compare + render**

Cell type: **Code**

```python
from IPython.display import Markdown, display

general = GeneralSpecialist(llm, logger)
review = await general.compare(specialist_outputs, fixture["question"])

lines = [f"### Question\n\n{fixture['question']}\n"]
lines.append(f"**Specialists compared:** {', '.join(specialist_outputs.keys())}\n")

lines.append(f"**Resolved contradictions ({len(review.resolved)}):**")
if not review.resolved:
    lines.append("- _(none)_")
for r in review.resolved:
    lines.append(f"- **{r.pair[0]} ↔ {r.pair[1]}** — {r.contradiction}")
    lines.append(f"  - Q: _{r.question_raised}_")
    lines.append(f"  - A: {r.answer}")
    lines.append(f"  - Conclusion: {r.conclusion}")

lines.append(f"\n**Open conflicts ({len(review.open_conflicts)}):**")
if not review.open_conflicts:
    lines.append("- _(none)_")
for c in review.open_conflicts:
    lines.append(f"- **{c.pair[0]} ↔ {c.pair[1]}** — {c.contradiction}")
    lines.append(f"  - Unresolved because: {c.reason_unresolved}")

lines.append(f"\n**Cross-domain insights ({len(review.cross_domain_insights)}):**")
if not review.cross_domain_insights:
    lines.append("- _(none)_")
for ins in review.cross_domain_insights:
    lines.append(f"- {ins}")

display(Markdown("\n".join(lines)))
```

- [ ] **Step 7: Cell 6 (code) — raw JSON dump**

Cell type: **Code**

```python
print(json.dumps(review.model_dump(), indent=2))
```

- [ ] **Step 8: Run the notebook top-to-bottom**

With `REGENERATE=False`, the hand-authored fixture (bureau vs spend_payments with conflicting tone) is fed to `compare`. Expected:
- Cell 4 prints "Loaded fixture: …" and lists `['bureau', 'spend_payments']`.
- Cell 5 renders at least one entry under Resolved, Open conflicts, or Cross-domain insights (depends on the LLM's call).
- Cell 6 prints the `ReviewReport` JSON.

If the LLM returns no contradictions at all (e.g., reads the two outputs as complementary rather than conflicting), that's a legitimate outcome — the polish signal is "did my edit change the classification?"

- [ ] **Step 9: Commit**

```bash
git add notebooks/test_compare_review.ipynb
git commit -m "feat(notebooks): add compare_review iteration notebook"
```

---

## Task 5: Build `test_data_query.ipynb`

**Files:**
- Create: `notebooks/test_data_query.ipynb`

Target: one specialist's `.run()` loop (from spec §4 — the choice locked in during brainstorming). Fixture specifies which specialist; the notebook instantiates that specialist via the registry and awaits its three-step chain.

- [ ] **Step 1: Create the notebook file**

VSCode → New File → `test_data_query.ipynb`.

- [ ] **Step 2: Cell 1 (markdown) — header**

Cell type: **Markdown**

```markdown
# Data Query — Iteration Notebook

Polish a single specialist's tool-calling loop against the data gateway.

**Edit surfaces:**
- Non-engineer: `skills/workflow/data_query.md`, `skills/workflow/data_catalog.md`, `skills/domain/<specialist>.md`, `config/data_profiles/*.yaml`.
- Engineer: `tools/data_tools.py`, `agents/base_agent.py`.

**Workflow:** edit a file above → Run All → read Cell 5's tool-call trace + specialist output → repeat. Cell 6's raw JSON is the regression signal.

**Switching specialists:** set `fixture["specialist"]` to any domain under `skills/domain/` (e.g., `bureau`, `wcc`, `modeling`, `crossbu`, `customer_rel`, `capacity_afford`, `spend_payments`).
```

- [ ] **Step 3: Cell 2 (code) — knobs + imports**

Cell type: **Code**

```python
# ═══════════════ KNOBS ═══════════════
FIXTURE = "basic_case"
REGENERATE = False
MODEL = "gpt-4.1"
# ═════════════════════════════════════

import json
import os
import sys
from pathlib import Path

import nest_asyncio
nest_asyncio.apply()

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.session_registry import SessionRegistry
from config.pillar_loader import PillarLoader
from data.catalog import DataCatalog
from data.gateway import SimulatedDataGateway
from data.generator import DataGenerator
from gateway.firewall_stack import FirewallStack
from gateway.llm_factory import build_llm
from logger.event_logger import EventLogger
from skills.domain.loader import load_domain_skill
from tools.data_tools import init_tools

FIXTURE_PATH = PROJECT_ROOT / "notebooks" / "fixtures" / "data_query" / f"{FIXTURE}.json"
print(f"Fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
```

- [ ] **Step 4: Cell 3 (code) — environment + gateway + catalog**

Cell type: **Code**

```python
logger = EventLogger(session_id="polish-data-query")
firewall = FirewallStack(logger=logger)
llm = build_llm(MODEL, firewall)

_DATA_TABLES_DIR = PROJECT_ROOT / "data_tables"
csv_gateway = SimulatedDataGateway.from_case_folders(str(_DATA_TABLES_DIR))
if csv_gateway.list_case_ids():
    gateway = csv_gateway
else:
    gen = DataGenerator(
        profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"),
        seed=42, cases=50,
    )
    gen.load_profiles()
    tables_raw = gen.generate_all()
    gateway = SimulatedDataGateway.from_generated(tables_raw)

catalog = DataCatalog(profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"))
init_tools(gateway, catalog, logger=logger)
registry = SessionRegistry()
pillar_loader = PillarLoader(pillar_dir=str(PROJECT_ROOT / "config" / "pillars"))
print(f"Available case IDs (first 5): {gateway.list_case_ids()[:5]}")
```

- [ ] **Step 5: Cell 4 (code) — fixture load or regenerate**

Cell type: **Code**

```python
if REGENERATE:
    current = {
        "question": "Summarise this customer's recent spend and payment behaviour.",
        "pillar": "credit_risk",
        "case_id": gateway.list_case_ids()[0],
        "specialist": "spend_payments",
        "notes": f"Regenerated from case {gateway.list_case_ids()[0]}, specialist spend_payments.",
    }
    FIXTURE_PATH.write_text(json.dumps(current, indent=2) + "\n")
    fixture = current
    print(f"Wrote fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")
else:
    fixture = json.loads(FIXTURE_PATH.read_text())
    print(f"Loaded fixture: {FIXTURE_PATH.relative_to(PROJECT_ROOT)}")

# Pick a usable case_id (same pattern as Task 2).
available = gateway.list_case_ids()
case_id = fixture["case_id"] if fixture["case_id"] in available else available[0]
if case_id != fixture["case_id"]:
    print(f"  (fixture case '{fixture['case_id']}' not available; using '{case_id}')")
gateway.set_case(case_id)

pillar_yaml = pillar_loader.load(fixture["pillar"]) or {}

skill = load_domain_skill(fixture["specialist"])
assert skill is not None, f"Unknown specialist {fixture['specialist']!r} — check skills/domain/."

print(f"Pillar: {fixture['pillar']} | Case: {case_id} | Specialist: {fixture['specialist']}")
print(f"Question: {fixture['question']}")
```

- [ ] **Step 6: Cell 5 (code) — run specialist + render tool trace**

Cell type: **Code**

```python
from IPython.display import Markdown, display

agent = registry.get_or_create(
    domain=fixture["specialist"],
    pillar=fixture["pillar"],
    domain_skill=skill,
    pillar_yaml=pillar_yaml,
    llm=llm,
    logger=logger,
)

# Mark where the trace starts so we can scrape only this run's events.
_TRACE_START = sum(1 for _ in open(logger._file_path)) if os.path.exists(logger._file_path) else 0

output = await agent.run(
    fixture["question"], mode="chat", root_question=fixture["question"],
)

# Scrape the logger's JSONL for tool-related events from this run onward.
tool_events = []
if os.path.exists(logger._file_path):
    with open(logger._file_path) as f:
        for i, line in enumerate(f):
            if i < _TRACE_START:
                continue
            evt = json.loads(line)
            if evt.get("event") in {"data_request", "data_response", "tool_call", "tool_result", "query_table", "get_table_schema", "list_available_tables"}:
                tool_events.append(evt)

lines = [f"### Question\n\n{fixture['question']}\n"]
lines.append(f"**Specialist:** `{fixture['specialist']}`  |  **Tables declared:** {', '.join(skill.data_hints) or '(none)'}\n")
lines.append(f"**Tool-related events ({len(tool_events)}):**")
if not tool_events:
    lines.append("- _(none captured — the logger may emit different event names in this codebase version)_")
for evt in tool_events:
    evt_type = evt.get("event", "?")
    payload = {k: v for k, v in evt.items() if k not in {"timestamp", "session_id", "trace_id", "event"}}
    lines.append(f"- `{evt_type}` — {json.dumps(payload)[:300]}")

lines.append(f"\n**Findings:**\n\n{output.findings}\n")
if output.evidence:
    lines.append("**Evidence:**")
    for e in output.evidence:
        lines.append(f"- {e}")
if output.implications:
    lines.append("\n**Implications:**")
    for i in output.implications:
        lines.append(f"- {i}")
if output.data_gaps:
    lines.append("\n**Data gaps:**")
    for g in output.data_gaps:
        lines.append(f"- {g}")
display(Markdown("\n".join(lines)))
```

- [ ] **Step 7: Cell 6 (code) — raw JSON dump**

Cell type: **Code**

```python
print(json.dumps(output.model_dump(), indent=2))
```

- [ ] **Step 8: Run the notebook top-to-bottom**

Expected:
- Cell 4 prints pillar/case/specialist.
- Cell 5 renders findings, evidence, implications, and (if any were logged) tool events. If `tool_events` is empty, that's OK — the logger event names for tool calls may differ from the hard-coded set; inspect `logs/polish-data-query.jsonl` to find the actual names and either add them to the set or accept the empty render. This is a polish decision, not a blocker.
- Cell 6 prints the `SpecialistOutput` JSON.

- [ ] **Step 9: Commit**

```bash
git add notebooks/test_data_query.ipynb
git commit -m "feat(notebooks): add data_query iteration notebook"
```

---

## Task 6: Documentation sweep (optional, zero-code)

**Files:**
- Modify: `notebooks/test_team_construction.ipynb` (Cell 1 only, if spec §4's "short header cell naming edit surfaces" is not already present — it is in this plan, so skip)
- Optional: append a brief paragraph to the repo root `README.md` under a new "Iteration notebooks" section.

- [ ] **Step 1 (optional): README pointer**

Add a short paragraph to `README.md`:

```markdown
### Iteration notebooks

Four notebooks under `notebooks/` support single-subsystem iteration for contributors polishing one part of the pipeline:

- `test_team_construction.ipynb` — specialist selection + sub-question decomposition
- `test_report_agent.ipynb` — curated-report ingestion
- `test_compare_review.ipynb` — cross-domain contradiction detection
- `test_data_query.ipynb` — a single specialist's tool-calling loop

Each notebook loads a JSON fixture from `notebooks/fixtures/<subsystem>/`, calls the subsystem, and renders the output. Non-engineers edit YAML/markdown under `skills/`, `config/`, or `reports/`; engineers additionally edit the Python.

See `docs/specs/2026-04-24-subsystem-iteration-notebooks-design.md` for the design.
```

- [ ] **Step 2 (optional): Commit**

```bash
git add README.md
git commit -m "docs: link to iteration notebooks from README"
```

---

## Self-review (implementer sanity check before handing back)

Before marking the plan complete, eyeball:

1. **All four notebooks open and Run All top-to-bottom without errors** against their committed fixtures (on a machine with `OPENAI_API_KEY` set).
2. **Each notebook is committed with output cells populated**, so `git log -p` on the notebook shows actual output content in the stored JSON.
3. **The four fixture directories each contain `basic_case.json`** and nothing else.
4. **No reference to a shared `polish.py` / helper module** anywhere — this was explicitly ruled out in the spec (Approach 2).
5. **The three existing notebooks (`test_chat_mode`, `test_data_access`, `test_report_mode`) are untouched** — check `git status`.

If any of the above fails, fix and recommit.
