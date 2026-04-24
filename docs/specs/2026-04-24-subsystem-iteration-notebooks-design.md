# Subsystem Iteration Notebooks — Design Spec

**Date:** 2026-04-24
**Depends on:** existing `Orchestrator` (`orchestrator/orchestrator.py`), `ReportAgent` (`agents/report_agent.py`), `GeneralSpecialist` (`agents/general_specialist.py`), `SessionRegistry` (`agents/session_registry.py`), `SimulatedDataGateway` (`data/gateway.py`), `DataCatalog` (`data/catalog.py`), workflow skills under `skills/workflow/*.md`.

## Goal

Give contributors — both Python engineers and non-engineer domain experts — a way to **iterate on one subsystem at a time** without running the full pipeline. Four subsystems need an iteration loop of their own:

1. **Team Construction** — `Orchestrator.plan_team()` (which specialists and what sub-questions).
2. **Report Agent** — `ReportAgent.run()` (curated-report ingestion).
3. **Compare / Review** — `GeneralSpecialist.compare()` (contradiction detection + cross-domain insights).
4. **Data Query** — a single specialist's tool-calling loop against `tools/data_tools.py`.

The delivery is **four self-contained Jupyter notebooks** under `notebooks/`, each following a fixed 6-cell skeleton. A non-engineer opens the notebook for the subsystem they want to polish, edits a YAML/markdown file (pillar config, workflow skill, domain skill, curated report), hits "Run All", and reads the rendered output. An engineer does the same plus edits Python.

No shared harness module, no CI-enforced golden outputs. Git diff on committed notebook output cells is the only regression signal.

## Non-goals

- Not replacing the existing `notebooks/test_chat_mode.ipynb`, `test_data_access.ipynb`, `test_report_mode.ipynb` — they stay as end-to-end smoke notebooks. The new notebooks live alongside them.
- Not building a CLI / Streamlit UI — notebook is the only interface.
- Not stubbing the LLM. Polishing prompts requires real LLM calls, so every run hits the configured model.
- Not enforcing a unified fixture schema across subsystems — each has its own shape because inputs differ.
- Not adding CI checks against fixture-derived outputs. No goldens, no pytest integration.
- Not extracting shared Python helpers into a module. Duplication across the four notebooks is accepted.

## §1 — File layout

```
notebooks/
├── test_chat_mode.ipynb              (existing, untouched)
├── test_data_access.ipynb            (existing, untouched)
├── test_report_mode.ipynb            (existing, untouched)
├── test_team_construction.ipynb      (new — pilot, built first)
├── test_report_agent.ipynb           (new — replicated after pilot)
├── test_compare_review.ipynb         (new — replicated after pilot)
├── test_data_query.ipynb             (new — replicated after pilot)
└── fixtures/
    ├── team_construction/
    │   └── <name>.json
    ├── report_agent/
    │   └── <name>.json
    ├── compare_review/
    │   └── <name>.json
    └── data_query/
        └── <name>.json
```

New notebooks use the existing `test_` prefix convention. Fixtures are grouped by subsystem; each fixture is a plain JSON file.

## §2 — Pilot notebook flow (`test_team_construction.ipynb`)

Six cells, self-contained. No imports from a shared notebook module; helpers are inlined as needed.

**Cell 1 — Knobs + imports.**
```python
FIXTURE = "basic_case"
REGENERATE = False
CASE_ID = "C000001"   # only used when regenerating
```
Plus `sys.path` fix for `notebooks/` → repo root, and imports for `Orchestrator`, `SimulatedDataGateway`, `DataCatalog`, `PillarLoader`, `build_llm`, `FirewallStack`, `EventLogger`, `SessionRegistry`, `DataGenerator`, `init_tools`, `list_domain_skills`.

**Cell 2 — Environment setup.** Build `logger`, `firewall`, `llm`, `registry`, load `.env`, load pillar YAML. ~15 lines, mirroring `main.py`'s session-start block minus argparse.

**Cell 3 — Gateway + catalog.** Build `SimulatedDataGateway` (CSV-first from `data_tables/`, generator fallback), call `gateway.set_case(fixture["case_id"])`, build `DataCatalog`, call `init_tools(gateway, catalog, logger)`. ~10 lines, mirroring `main.py`.

**Cell 4 — Fixture load / regenerate.**
- If `REGENERATE=False`: read `notebooks/fixtures/team_construction/<FIXTURE>.json` into a dict.
- If `REGENERATE=True`: build a dict from cell 1's values (plus `question` and any overrides the user sets in this cell), write it to the fixture path, then continue as if loaded.

**Cell 5 — Call + render.**
```python
orchestrator = Orchestrator(llm, logger, registry, fixture["pillar"],
                            pillar_config=pillar_yaml, catalog=catalog)
plan = await orchestrator.plan_team(
    question=fixture["question"],
    available_specialists=fixture["available_specialists"] or list_domain_skills(),
    active_specialists=fixture["active_specialists"],
)
```
Rendered via `IPython.display.Markdown`:
- Root question + pillar.
- Selected specialists, each with their `data_hints` tables.
- Per-specialist sub-question, with quick visual pairing.
- Specialists that were available but not picked (so reviewers can spot under-selection).

**Cell 6 — Raw output dump.** Pretty-printed JSON of `[p.model_dump() for p in plan]`. This is the cell whose output is the regression signal: after an edit, "Run All" and eyeball the diff against the committed notebook state.

**Non-engineer workflow:** open notebook → edit `skills/workflow/team_construction.md` or `config/pillars/credit_risk.yaml` in a side tab → "Run All" → read cell 5. They only touch cell 1 to swap fixtures.

**Engineer workflow:** same, plus they may edit `orchestrator/orchestrator.py` (e.g., `_select_team`'s prompt-building) between runs.

## §3 — Fixture format

Each fixture is `notebooks/fixtures/<subsystem>/<name>.json`. Shape is **subsystem-specific** — no forced common schema.

**`team_construction` fixture:**
```json
{
  "question": "Is this customer's credit risk acceptable?",
  "pillar": "credit_risk",
  "available_specialists": null,
  "active_specialists": [],
  "case_id": "C000001",
  "notes": "baseline multi-specialist case"
}
```
- `available_specialists: null` → use everything `list_domain_skills()` returns.
- `active_specialists: []` → almost always empty for polish runs.
- `case_id` is consumed by cells 2–3 to build the gateway/catalog; `plan_team` itself doesn't see it.
- `notes` is free-text for humans.
- Regeneration for this subsystem is trivial: cell 4 serializes cell 1's values back to disk.

**`report_agent` fixture:** `{question, case_id, notes}`. Case folder contents are read live from `reports/<case_id>/` at run time; regeneration is just picking a case-id whose `reports/` folder is already populated.

**`compare_review` fixture:** `{question, specialist_outputs: {<domain>: {findings, evidence, implications, data_gaps}, ...}, notes}`. The `specialist_outputs` dict is the expensive-to-author part. **Regeneration for this notebook is a partial team run**: dispatches the specialists using the real orchestrator path, captures the four `SpecialistOutput` values, serializes them into the fixture JSON.

**`data_query` fixture:** `{question, pillar, case_id, specialist, notes}`. `specialist` is the domain whose `.run()` loop is being polished (e.g., `"bureau"`, `"wcc"`). Regeneration just updates the question/specialist fields.

Fixtures are git-tracked. When upstream schemas evolve (e.g., a new required field on `SpecialistOutput`), flip `REGENERATE=True` in each affected notebook and re-run — this is the "migration" tool.

## §4 — Replication plan for the other three subsystems

After `test_team_construction.ipynb` is validated, each of the other three is a copy-paste-adapt of the pilot. The 6-cell skeleton is fixed — only cells 4 (fixture), 5 (call + render), and 6 (raw dump) diverge.

### `test_report_agent.ipynb`

- **Entry point:** `ReportAgent(llm, logger).run(fixture["question"], _REPORTS_DIR / fixture["case_id"])` → `ReportDraft`.
- **Render focus:** `coverage` (`full` / `partial` / `none`), `answer`, `files_consulted`, `evidence_excerpts` side-by-side with the answer.
- **Edit surfaces (non-engineer):** `skills/workflow/report_needle.md`, `skills/workflow/report_analysis.md`, or the curated `reports/<case_id>/*.md` themselves.
- **Edit surfaces (engineer):** `agents/report_agent.py`.

### `test_compare_review.ipynb`

- **Entry point:** `GeneralSpecialist(llm, logger).compare(specialist_outputs, fixture["question"])` → `ReviewReport`.
- **Render focus:** `resolved` contradictions, `open_conflicts`, `cross_domain_insights`, `data_requests_made` — grouped by the specialist pair involved.
- **Regeneration specifics:** rebuild the `specialist_outputs` dict by running the full team-dispatch path from `Orchestrator._run_team_workflow` up through specialist completion, then snapshotting each `SpecialistOutput` as JSON into the fixture.
- **Edit surfaces (non-engineer):** `skills/workflow/comparison.md`.
- **Edit surfaces (engineer):** `agents/general_specialist.py`.

### `test_data_query.ipynb`

- **Entry point:** a single specialist's tool-calling loop:
  ```python
  skill = load_domain_skill(fixture["specialist"])
  agent = registry.get_or_create(
      domain=fixture["specialist"],
      pillar=fixture["pillar"],
      domain_skill=skill,
      pillar_yaml=pillar_yaml,
      llm=llm,
      logger=logger,
  )
  output = await agent.run(
      fixture["question"], mode="chat", root_question=fixture["question"],
  )
  ```
- **Render focus:** tool-call trace scraped from the `EventLogger` (table queried, filters, row counts, any data-catalog references) plus the final `SpecialistOutput` fields.
- **Edit surfaces (non-engineer):** `skills/workflow/data_query.md`, `skills/workflow/data_catalog.md`, `skills/domain/<specialist>.md`, `config/data_profiles/*`.
- **Edit surfaces (engineer):** `tools/data_tools.py`, `agents/base_agent.py`.

### "Done" criteria per notebook

- 6-cell skeleton present and runs top-to-bottom without errors against the bundled fixture.
- At least one fixture committed under `notebooks/fixtures/<subsystem>/`.
- Notebook committed with output cells populated from one successful run, so future diffs are meaningful.
- Short header cell at the top of each notebook (plain markdown) names the edit surfaces for non-engineers.

No CI check, no goldens, no pytest wiring.

## §5 — Open questions

None — all scoping questions resolved during brainstorm. Any ambiguity in the data-query specialist choice (which `specialist` value to ship the first fixture with) is an implementation-time call, not a design-time one.
