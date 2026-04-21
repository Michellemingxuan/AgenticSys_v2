# Agentic Case Review — Pipeline

One question in → one synthesized answer out. Every piece is swappable.

## Pipeline stages

| # | Stage | Role | Key input | Key output |
|---|-------|------|-----------|------------|
| 1 | **Chat Agent** *(entry)* | receive question | reviewer question | pass-through |
| 2 | **Team Constructor** | plan | question + Data Catalog | selected specialists |
| 3 | **Specialists** *(7 domains)* | analyze | skill + pillar + data | findings · evidence · data gaps |
| 4 | **General Specialist** | cross-review | all specialist outputs | resolved / open conflicts · insights |
| 5 | **Orchestrator** | synthesize | findings + review | `FinalOutput` |
| 6 | **Chat Agent** *(format)* | render | `FinalOutput` | reviewer-facing answer |

Every specialist runs the same **3-step chain**: **Data → Synthesize → Answer** (or **Report**, in report mode).

## Specialists

| Name | Focus | Data table(s) |
|------|-------|---------------|
| `bureau` | Tradelines, derogs, credit scores | `bureau` |
| `crossbu` | Multi-product overlap, contagion | `xbu_summary` |
| `modeling` | Model scores, trajectory, drivers | `model_scores` |
| `spend_payments` | Monthly volumes, payment returns | `txn_monthly`, `spends`, `payments` |
| `wcc` | Watch-list / compliance flags | `wcc_flags` |
| `customer_rel` | Tenure, product breadth, segment | `cust_tenure` |
| `capacity_afford` | Income, DTI, repayment capacity | `income_dti` |

## Supporting layer

- **Data Catalog** — YAML profiles; source of truth for table/column semantics. Consulted by Team Constructor and each specialist.
- **Data Gateway** — simulated data scoped to the active case. Tools: `list`, `schema`, `query`.
- **Session Registry** — keeps specialists warm across turns; each maintains a rolling summary.
- **Firewall Stack** — wraps every LLM call. Retries on content blocks, masks long digit runs, blocks PII / role injection.
- **LLM Adapter** — pluggable backend: OpenAI (dev) or SafeChain (deployment).
- **Pillar overlay** — one of `credit_risk` / `escalation` / `cbo`. Sets focus, cut-off date, and per-specialist report format.

## Two modes

- `chat` — Team Constructor filters to relevant specialists; specialists emit conversational answers.
- `report` — all 7 specialists run; outputs follow pillar-specific report rules.

## Walk-through · Q1 + follow-up

> **Q1.** “How frequent were positive events (e.g. limit increases) in the last 18 months?”
> Case `CASE-00001` · Pillar `credit_risk` · Mode `chat`.

1. **Reviewer asks Q1.** Chat Agent passes it through.
2. **Team Constructor reads the Data Catalog.** Matches `"positive events"` → `model_scores.positive_events` → **invokes `modeling`**.
3. **`modeling` runs** its 3-step chain:
   - **Data** — `query_table('model_scores', columns='trans_month,positive_events')`
   - **Synthesize** — count months with events in last 18.
   - **Answer** — chat-style findings, evidence, data gaps.
   - Stays warm in Session Registry.
4. **Reviewer asks follow-up:** “Are the positive events consistent with the bureau scores?”
5. **Team Constructor re-runs.** Warm `modeling` is preferred; catalog matches `"bureau scores"` → `bureau.{fico_score, sbfe_score, ...}` → **additionally invokes `bureau`**.
6. **Both specialists run.** `bureau` is cold → skill + pillar injected fresh. `modeling` carries its rolling summary from Q1.
7. **General Specialist** compares `modeling` ↔ `bureau` outputs pairwise → resolves or flags conflicts.
8. **Orchestrator** merges into `FinalOutput` (answer + data-gap assessments + conflicts).
9. **Chat Agent formats** and prints to the reviewer.

## Design properties

- **Catalog-driven specialist selection.** Team Constructor's only input about specialists is their tables + columns. New table → catalog entry → automatically visible to the planner.
- **One Base Specialist Agent**, parametrized by domain skill + pillar YAML. New specialist = one skill file. New pillar = one YAML.
- **Absence-as-signal.** Orchestrator explicitly reasons about missing data (e.g. no bureau record → thin-file risk).
- **Warm specialists across turns.** Session Registry preserves rolling summaries so follow-ups reuse prior context.
- **Firewall everywhere.** Every LLM call passes through retry + sanitize — no bypass path.
