# Catalog Viewer Page — Design (slice 3)

**Date:** 2026-06-22
**Status:** Design (approved via brainstorming)
**Builds on:** the profile-reconciliation-agent feature (slices 1+2). Lets the administrator *see* the profile/context/provenance state and trigger a reconcile from the browser — co-located with the existing node_trace viewer.

## Goal
A `/catalog` page added to the existing trace-viewer Flask app (port 3002) that (a) **monitors** data profiles + context dictionaries + provenance + last-reconcile flags via cheap file reads, and (b) **triggers** `--reconcile` (subprocess) and shows the structured results.

## Architecture
- **New module** `tools/node_trace/catalog_page.py` exposing `register_catalog_routes(app)`; `tools/node_trace/viewer.py` calls it once so the routes join the SAME Flask `app` on port 3002 (same `_start_trace_viewer` launcher — no rename). `viewer.py` stays lean; all catalog logic lives in the new module.
- A small shared top-nav links **Traces ⇄ Catalog** (added to the catalog page and the existing trace pages).
- Routes: `GET /catalog` (dashboard), `POST /catalog/reconcile` (trigger).
- HTML via `render_template_string`, mirroring viewer.py's existing inline style for visual consistency (no new CSS framework).

## Monitoring view (`GET /catalog`) — cheap reads only, no LLM, no gateway
A pure builder `build_catalog_view(profile_dir, context_dir, provenance_path) -> dict` assembles the display model. Per table (from `DataCatalog` over `config/data_profiles`):
- table name, description, aliases;
- each column: dtype, parse_hint, description, threshold (`risk_threshold` + `risk_direction`), and a **provenance badge** — `human` / `agent` / `unmanaged` (from `.provenance.json`: a field present with a baseline == current → agent; present and differing → human; absent → unmanaged);
- **context coverage**: per profile column, whether a context-dict entry exists (✓/✗); plus **context-only** vars (in the dictionary but not in the profile);
- **last reconcile result**: if a persisted summary exists, show its timestamp, counts, and flag list.

Reads only `config/data_profiles/*.yaml` (via `DataCatalog`), `context/*.txt` (via `context_dict.load_context_by_table`), `.provenance.json`. Real-column coverage + schema-divergence require data and therefore appear only in reconcile *results*, not the static view.

## Trigger (`POST /catalog/reconcile`) — subprocess (approach A)
- Runs `python -m datalayer.sync --reconcile --json <tmp>` as a subprocess (default: real LLM, honoring `LLM_BACKEND`; a **page toggle** adds `--no-llm` for a deterministic preview). Isolates the LLM/safechain stack from the lite viewer; reuses the exact tested CLI.
- A new `--json <path>` option on the reconcile CLI dumps the structured `ReconcileResult` (`writes`, `context_writes`, `flags`) to `<path>`; the endpoint reads it, persists a small summary (timestamp + counts + flags) to a results JSON the `/catalog` page reads for "last run", and returns it.
- v1 runs synchronously with a spinner + a timeout; background+poll is a later refinement if reconcile gets slow.

## Out of scope (slice 3)
- In-browser editing of profiles/context (manual edits stay file-based).
- Background/streaming reconcile (v1 is synchronous).
- Auth (inherits whatever the trace viewer already has — none beyond network scoping).

## Testing
- `build_catalog_view` unit-tested with tmp fixtures (profiles + context + provenance → expected model); the autouse guard fixture protects real `context/`.
- The `--json` CLI option tested (run_reconcile writes the structured file).
- Route tests via Flask test client: `GET /catalog` renders the model; `POST /catalog/reconcile` invokes the subprocess **mocked** (no real reconcile, no LLM in tests).

## Open questions
- Synchronous-with-timeout is fine for the expected table/column counts; revisit if a full reconcile exceeds ~30s on real data.
