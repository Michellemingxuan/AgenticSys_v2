# Profile Reconciliation Agent — Design

**Date:** 2026-06-22
**Status:** Design (pending review)
**Workstream:** A of three polish directions (A = this; B = read-vs-query reasoning policy; C = warm-start / default journey). A is foundational — B and C2 read through the profiles A maintains.

## Problem

`config/data_profiles/*.yaml` are the canonical schema/semantics the catalog (`datalayer/catalog.py`) injects into LLM prompts. Real data tables drift over time and through human edits (renames, dtype shifts, added/removed columns), and the authoritative variable knowledge lives in **context dictionaries** (`context/*_context_description.txt`) that don't line up 1:1 with the tables — they cover more or fewer variables. Keeping profiles correct by hand is the recurring breakage class (e.g. the `txn_monthly` removal that rippled through tests, skills, pillar, and settings).

This is not a one-off migration. Banking variable sets churn continuously — scorecards re-versioned, drivers added/retired, source columns renamed, dtypes/thresholds revised. The real need is a **standing, repeatable adaptation loop** the administrator can re-run as variables change, where each run's flags surface what moved and prior human curation is preserved.

Existing scaffolding does **table ↔ profile** reconciliation: `datalayer/adapter.py` (fuzzy `match_column`, dtype/category drift, `audit_profile_only`, `aggregate_diffs`) driven by an interactive CLI `datalayer/sync.py`. It does **not** ingest the context dictionaries, and its per-column interactive review is heavier than wanted.

## Goal

A **manager-triggered, agent-assisted reconciliation** that, on command, reconciles the three sources for each table and **auto-writes** converged profiles, using git as the audit/rollback trail. It treats per-case schemas as homogeneous and **flags** divergence rather than averaging it.

## Sources (three-way reconciliation, per table)

1. **Live table columns** — `data_tables/real/<case_id>/<file>.csv`, observed across all cases. Real file names ≠ canonical (`modelling_data.csv`→`model_scores`; `payments_success.csv`+`payments_returns.csv`→`payments`); the gateway/adapter already carries this mapping.
2. **Canonical profile** — `config/data_profiles/<table>.yaml` (the stable artifact the agent writes).
3. **Context dictionary** — `context/<domain>_context_description.txt`. **Domain-grouped**, so one file can cover several tables (`modeling_context` → `model_scores` + `model_scores_transaction`; `crossbu` → cards + merchants). Each line: `var_name: description. Values above/below X are risky.` — carries **description AND threshold**.

New static input: a **context-file → table(s) map** (small hand-maintained dict, since it's domain→several-tables, not 1:1).

## Stability model

- Cases are **expected to be schema-homogeneous** — same tables, same columns, same dtypes across all cases.
- The agent's cross-case role is **consistency verification, not statistical convergence/voting.** When cases agree, reconcile the single uniform schema. When a case diverges, **flag it** (which case, which table/column) and hold that table back from auto-write.
- Profiles are stable artifacts: they adapt across reconciliation runs and converge; re-running on unchanged inputs is a **no-op (idempotent)**.
- Reconciliation runs **only on the manager's order** — never per-case, never at query time (`adapter.py` is already sync-time-only).

## Human edits & provenance protection

The administrator can hand-edit `config/data_profiles/*.yaml` directly for small fixes, and the agent must **never revert a human correction** on a later run.

Mechanism — **provenance auto-detect** (no manual marking):
- When the agent writes a field (description, threshold, dtype, column mapping), it records the value it wrote as that field's provenance baseline.
- On the next run, before touching a field, the agent compares the current value to its recorded baseline:
  - **Equal** → the agent still owns it → it may update it (and refresh the baseline).
  - **Different** → a human edited it → the agent **leaves it untouched**, marks it human-owned, and stops managing it. If new evidence (data/context) conflicts with the human value, it **flags** the conflict rather than overwriting (consistent with the design's flag-don't-clobber philosophy).
- Result: you just edit the YAML; edits stick. Idempotence still holds — agent-owned-unchanged and human-owned fields both produce no write on re-run.

Provenance **storage mechanism** (inline per-field baseline block vs. a sidecar file vs. value-hashes) is deferred to the implementation plan; the *behavior* above is the contract. Prompt-facing fields must stay clean (provenance lives in a clearly separated block or sidecar, not inline in the description text).

## Approach — hybrid (agent matches, deterministic validates)

Earlier assumption (deterministic-first, agent-on-leftovers) was inverted: opaque real column names (`cust_eff_se_cdss_5_180_day_score`) won't reliably match by string rules.

- **Agent = primary matcher + description polisher + threshold parser** (the judgment-heavy work).
- **Deterministic code = guardrail/validator + cheap exact wins + flag generator.** It claims exact/known-alias matches, and **validates every proposed match** (dtype compatibility via `adapter._dtype_compatible`, sample-value/category sanity via `_observe_categories`) before any write.
- **Auto-write gate:** a change lands only if **agent-confident AND it passes deterministic validation**; otherwise it becomes a flag. This keeps auto-apply safe even though the agent does most matching.

## The small agent's boundary

Three jobs, on leftovers the deterministic layer didn't already settle:

1. **Matching** — propose column↔canonical and context-var↔column alignments for opaque names, with a confidence score.
2. **Description polish** — when a context description is vague / too-general / empty, rewrite for clarity *preserving substance*; crisp descriptions pass through verbatim. Grounded by an injected **internal-knowledge brief** (CDSS = `credit_loss_prob`, TSR = `tot_struct_risk_score`, plus the existing pillar vocabulary glossary) so polish reflects house concepts, not generic guesses.
3. **Threshold parse/normalize only** — read the threshold out of the messy context wording (`Values above 5.8 are risky`, `Scores from 10-100`, `on or above 1`) into a structured `{operator, value|range}`. **Never invents or changes the numeric value** — context is the gold-standard source; the agent only normalizes phrasing.

The agent may **never**: change a threshold value, alter a clear description's meaning, auto-resolve a cross-case schema inconsistency, or overwrite a human-corrected field (all are flags, not writes).

## Pipeline (one manager-triggered run)

1. **Cross-case consistency check (deterministic).** Verify every case has the same table set and, per table, the same columns + dtypes. Divergence → flag + hold that table back. Consistent → carry the uniform schema forward.
2. **Cheap exact matches + validation setup (deterministic).** Claim exact/normalized/known-alias column matches; gather dtype + sample values per column for later validation.
3. **Context parse (deterministic).** Parse mapped context file(s) → `{var: raw_description, raw_threshold_text}`.
4. **Agent pass (leftovers).** For unmatched columns/vars: propose matches (+confidence), polish vague descriptions, normalize threshold text → structured form.
5. **Validate + gate (deterministic).** Each agent proposal must pass dtype/sample validation. Confident + valid → stage for write; else → flag.
6. **Provenance gate (deterministic).** For each staged field, compare the current profile value to the agent's recorded baseline. Agent-owned (equal) or never-written → write. Human-edited (differs) → skip the write; if the staged value conflicts with the human value, emit a conflict flag.
7. **Partial-overlap outcomes:** table column with no context entry → flag *table-only (no dictionary)*; context entry with no column → flag *context-only (dictionary covers a missing var)*; matched → write description (+ structured threshold).
8. **Auto-write** the surviving staged changes to `config/data_profiles/*.yaml` (idempotent) and refresh provenance baselines for fields the agent wrote. Emit a **concise flag list** — no per-column report.

## Trigger, output, environment

- **Trigger:** an agent/auto mode on `python -m datalayer.sync` (reuse its plumbing) — e.g. `--agent --auto-apply`, no interactive prompts.
- **Write:** auto-write to `config/data_profiles/*.yaml`; `git diff` is the audit/rollback (these files are git-tracked).
- **Flags (stdout, grouped, concise):** (a) cross-case schema inconsistencies; (b) low-confidence / unresolved matches; (c) context-only vars; (d) table-only columns; (e) human-set value conflicts with current evidence.
- **Environment:** the agent's LLM call honors `LLM_BACKEND` (OpenAI in dev, safechain in prod) like the rest of the system. It is an offline ops tool — runnable in either environment by the manager.

## Testing

- **Deterministic parts fully unit-tested:** consistency check flags divergence; context parser extracts `{var, description, threshold}` across the varied phrasings; exact/alias matching; dtype/sample validation; the four partial-overlap outcomes.
- **Agent step mocked** (per the project's "mock confirmed safechain behavior in dev tests" rule): test the confidence+validation gate (confident&valid → apply; uncertain or invalid → flag).
- **Invariant test:** the agent path **never changes a threshold's numeric value** — only its structured representation.
- **Provenance tests:** a human-edited field survives a re-run untouched; an agent-owned (unchanged-since-write) field is still updatable; a human value that conflicts with new evidence produces a conflict flag, not a write.
- **Golden idempotence test:** fixed tables + context + profile → expected profile + flag list; a second run is a no-op.

## Out of scope (this slice)

- Category-vocabulary reconciliation and pillar-glossary editing (full-semantic; deferred).
- Runtime drift resilience (query-time graceful degradation) — a separate possible slice.
- Workstreams B (read-vs-query policy) and C (warm-start / default journey).

## Open questions

- **Context-file → table map:** hand-maintained dict to start; revisit if it grows unwieldy.
- **Confidence threshold** for the agent's auto-apply gate: start with a tunable constant alongside the existing `FUZZY_THRESHOLD` / `--auto-threshold`; calibrate against the real case once measured.
