# Transaction-level data support — design

**Date:** 2026-06-08
**Status:** Approved in shape (workstreams A + B); awaiting spec review → implementation plan.
**Scope decision:** This spec covers **A (data profiles)** + **B (agent instructions)** only. The two code-fix workstreams surfaced during diagnosis — **C (filter robustness)** and **D (`make_chart(kind="table")` render fix)** — are deferred to a **separate follow-up spec** (`docs/superpowers/specs/2026-06-08-filter-and-table-render-fixes-design.md`, not yet written). They are documented at the end of this file as "Deferred" so the diagnosis isn't lost.

---

## 1. Problem & goal

Two new **transaction-level** data tables now ship in the per-case folders (one row = one transaction), alongside the existing **monthly-level** tables (one row = one month, aggregated):

| Monthly (exists) | Transaction-level (new) | Real CSV file |
|---|---|---|
| `model_scores` (`modelling_data`) | `model_scores_transaction` | `modelling_data_transaction.csv` |
| `score_drivers` (`score_drivers_data`) | `score_drivers_transaction` | `score_drivers_data_transaction.csv` |

The transaction tables carry the **same underlying features** as their monthly siblings, but:
- Monthly rows aggregate each feature over the month (canonical columns carry `_max` / `_mean` / `_min` aggregation suffixes).
- Transaction rows carry the **raw per-transaction value** of each feature (the un-suffixed base name), plus **four transaction-specific columns**: `trans_dt`, `txn_date_time`, `appr_deny_cd`, `auto_decline_pos_deny_cd_s1`.

The system must:
1. **Add data profiles** for the two new tables (so the catalog/schema tools describe them).
2. **Instruct agents** on the routing principle: **transaction-level data answers transaction-level questions** (e.g. "was the customer out of pattern when he made the grocery transactions?", "why was this transaction declined?"); **monthly-level data answers trend/trajectory questions**.
3. Reinforce **high-quality filtering** for these high-volume tables (accurate time range via `txn_date_time` / `trans_dt`, accurate entity values), and — per user — make filter-caution a **cross-cutting** instruction for *all* questions, because the current failure mode is "zero records due to value/format mismatch," not just volume.
4. For transaction-level questions, **surface the relevant transaction rows in the final response** (tables appear frequently).

No gateway/loader changes are required: `LocalDataGateway.from_case_folders` auto-discovers any `*.csv` in a case folder, and `DataCatalog` auto-loads every YAML in `config/data_profiles/`.

---

## 2. Non-goals (this spec)

- **No filter-engine code changes** (`tools/data_tools.py`). The `eq` case/whitespace sensitivity, missing `contains` op, numeric-coercion over-eagerness, `ne`-drops-nulls, fuzzy-column mis-bind, and `MMM-YY` / Excel-serial date gaps are all **deferred to C** below.
- **No `make_chart(kind="table")` render fix** (`data_viz_tools.py` / `redacting_tool.py` / `data_viz.md`). Deferred to D.
- No new specialist agent. The transaction tables are owned by the **existing modeling specialist** (decision below).
- No frontend changes.

---

## 3. Workstream A — Data profiles

Two new YAML files in `config/data_profiles/`, authored to match the existing profile format exactly (see `config/data_profiles/model_scores.yaml` as the template).

### A1. `model_scores_transaction.yaml`

```yaml
table: model_scores_transaction
description: "<as below>"
rows_per_case: <large; e.g. 5000 — one row per transaction>
one_row_per_case: false
aliases:
- modelling_data_transaction
- modeling_transaction
columns:
  trans_dt: { dtype: date, ... }
  txn_date_time: { dtype: date, parse_hint, ... }
  appr_deny_cd: { dtype: int/categorical, ... }
  auto_decline_pos_deny_cd_s1: { dtype: string/categorical, ... }
  <feature columns — see authoring rule>
```

**Description block** (mirror the monthly one, but state the grain):
> Transaction-level model scoring features — **one row per transaction** (contrast `model_scores`, which is one row per month). Holds the same ML output scores (CDSS `credit_loss_prob`, TSR `tot_struct_risk_score`, `cbr_score`, `gam_clr_erly_risk_score`, …) and input features as the monthly table, but at the raw per-transaction grain (no `_max`/`_min`/`_mean` aggregation). Use this table for **transaction-level questions** (was a specific transaction out of pattern, why was a transaction approved/declined, what did the scores look like at the moment of a transaction). Use the monthly `model_scores` for **trend/trajectory** questions. This table is **high-volume — always filter tightly** (narrow `txn_date_time` / `trans_dt` range; specific entity values) before querying. Same CSV wire-format quirks apply (quoted numerics, "thousands"/"millions" suffixes, comma thousands).

**Transaction-specific columns** (documented in full):

| Column | dtype | description |
|---|---|---|
| `trans_dt` | date | Transaction date, `YYYY-MM-DD`. **This is the default time-range filter key** — use it for almost all date windows (day/month/quarter). |
| `txn_date_time` | date | Transaction timestamp with time component, e.g. `2024-01-01 15:25:20.602`. **Do not default to this for retrieval.** Reach for it only when the question genuinely needs sub-day precision (within-day ordering, time-of-day patterns, the exact moment of a purchase). Filtering on a full timestamp is brittle and itself a zero-record risk. **`eq` on a bare date drops the time on both sides** (handled by `_date_key`), so day-level `eq`/range works; do not expect to match a specific sub-second timestamp. |
| `appr_deny_cd` | int (categorical) | Authorization decision: **`0` = approved, `1` = declined.** Filter with `eq` and value `0` or `1`. |
| `auto_decline_pos_deny_cd_s1` | string (categorical) | Decline-reason code. Populated **only when `appr_deny_cd = 1`** (blank/null on approvals). Quote the raw code in findings; do not invent a human label unless the schema/catalog provides one. |

**Feature-column authoring rule** (mechanical):
- For each feature column present in `modelling_data_transaction.csv`, take the **canonical base name** from `model_scores.yaml` (in the monthly profile the base name is already canonical and the `_max`/`_min`/`_mean` variant is listed under `aliases`). In the transaction profile, the **canonical name = the base name = the CSV column name**; **drop the aggregation-suffix aliases** (they don't exist in the txn CSV).
- **Reuse the monthly description, `dtype`, `risk_threshold`, `risk_direction`** verbatim (these describe the feature, which is identical). Drop the synthetic-generation stats (`distribution`/`mean`/`std`/`min`/`max`) — they're for the data generator and irrelevant for a real-CSV-backed table (omit, matching how `spends.yaml` keeps them only where generation is wanted; safe to omit here).
- Columns confirmed present in the txn CSV header (canonical base names): `cust_lexis_nexis_tot_tax_assess_val_am`, `cust_cash_tot_liab_yr1_rt`, `cbr_score`, `cust_eff_se_cdss_5_180_day_score`, `cust_expsr_avg_rem_12m_ratio`, `se_no_norm_wtd_pd_unpaid_amt`, `tpf_internal_delinq_idx`, `oop_interaction`, `sum_tot_rsky_evnt`, `cust_rnn_score`, `cust_experian_trans_union_inq_idx`, `hcam_bal_trnd_indx`, `avutil_exrvlv_balgt50`, `cust_expr_to_arb_inc_ratio`, `lvrg_debt_remit`, `cust_intr_extnl_unscr_tt_debt_srvc_rt1`, `cust_lndexpsr_minloc_6m_ratio`, `tot_cons_comm_trds_g75`, `cust_pymt_chan_risk_score`, `exp_pif`, `cust_debt_pymcpty_tm_inc`, `positive_events`, `times_30_dpd`, `cust_min_due_12mo_avg`, `cust_enhnc_one_way_spend_concentration_30day_rt1`, `gam_mtge_loan_actl_pymt_am`, `cust_ext_delinq_idx`, `time_wtd_return_index`, `avg_remit_minus_max`, `credit_loss_prob`, `tot_struct_risk_score`, `ons_30_trd`, `last_cycle_cut_revolve_rate`, `hcam_src_trnd_indx`, `tpf_cust_hi_rvlv_line_am`, `cust_lend_acct_paydown`, `cust_open_acct_paydown`, `gam_clr_erly_risk_score`, `cust_lexis_nexis_blended_score`, `cust_sbfe_score`, `cust_dnb_paydex_score`.
- **Txn-only columns not in the monthly profile** (document briefly, mark `description_pending: true` if meaning unknown): `one_expsr_usd_currency_amt` (one-exposure USD currency amount), `exp_pif` (Pay-In-Full exposure, raw — monthly canonical is `exp_pif_max`), `cbr_score` (credit bureau score, raw — monthly canonical is `cbr_score_max`), `tot_cons_comm_trds_g75` (note monthly canonical is `tot_cons_comm_trds_g30` with `_g75_max` alias; txn uses the `g75` base — describe as "total consumer & commercial trades > 75").
- **Implementation aid:** generate the column list by diffing the txn CSV header against `model_scores.yaml` canonical keys + their aliases, rather than hand-typing — reduces transcription error. Verify every CSV header column ends up in the profile.

### A2. `score_drivers_transaction.yaml`

```yaml
table: score_drivers_transaction
description: "<as below>"
rows_per_case: <large>
one_row_per_case: false
aliases:
- score_drivers_data_transaction
columns:
  index: { dtype: int, description: row index }
  grms_cid: { dtype: string, description: customer id (GRMS CID) }
  trans_dt: { ... }          # same as A1
  txn_date_time: { ... }     # same as A1
  appr_deny_cd: { ... }      # same as A1
  auto_decline_pos_deny_cd_s1: { ... }  # same as A1
  top_cdss1..5: { dtype: string, description: "Nth strongest feature pushing the CDSS score UP for this transaction (feature name from model_scores)" }
  bottom_cdss1..5: { dtype: string, description: "Nth strongest feature pushing the CDSS score DOWN for this transaction" }
  top_tsr1..5 / bottom_tsr1..5: { ... same, for TSR }
```

**Description block:** mirror `score_drivers.yaml` but state grain — "per-transaction snapshot of the top-5 / bottom-5 features driving the CDSS and TSR scores **for each transaction** (contrast `score_drivers`, which is per-month). Use for 'which features drove the score on this specific transaction / decline'; use monthly `score_drivers` for driver-rotation-over-time questions." Carry the same `appr_deny_cd` / decline-reason documentation.

### A3. Profile-loading sanity check (acceptance)

After authoring, confirm:
- `DataCatalog` loads both YAMLs without error (`get_schema("model_scores_transaction")`, `get_schema("score_drivers_transaction")` return non-empty).
- Table-name resolution maps the alias → real CSV: `list_available_tables()` / `get_table_schema("model_scores_transaction")` for the real case (`366132845011`) resolves to `modelling_data_transaction.csv` and lists the real columns.
- No regression to existing monthly profiles.

---

## 4. Workstream B — Agent instructions

### B1. `skills/workflow/data_query.md` — general routing + filter rigor (all specialists)

Add two short sections (this skill is owned by `base_specialist`, so every specialist sees it):

**(a) Transaction-level vs monthly-level data.**
> Some domains have both a **monthly** table (one row per month, aggregated) and a **transaction** table (one row per transaction). Choose by question type:
> - **Transaction-level questions** → transaction tables (`spends`, `model_scores_transaction`, `score_drivers_transaction`). Examples: "was this transaction out of pattern?", "why was the customer declined on <date>?", "show the grocery transactions last month", "what did the risk scores look like at the moment of purchase?".
> - **Trend / trajectory / over-time questions** → monthly tables (`model_scores`, `score_drivers`, `txn_monthly`, …). Examples: "is risk deteriorating?", "how has CDSS moved over 12 months?".
> - When in doubt and both could apply, prefer the **monthly** table for aggregate/trend framing and the **transaction** table only when the answer hinges on specific transactions.

**(b) Filter rigor (applies to every query, not just high-volume tables).**
> A zero-record result is **more often a filter mismatch than a true absence**. Before concluding "no data":
> - **Check the column's actual format first** via `get_table_schema` — never assume a date format or a value spelling. Match the column's own format (e.g. `txn_date_time` is `YYYY-MM-DD HH:MM:SS.fff`; `appr_deny_cd` is integer `0`/`1`).
> - For **free-text entity columns** (merchant name, reason codes), exact `eq` is brittle (case/whitespace). If an exact match returns nothing, re-query with a broader value or fewer constraints before giving up. *(Note: a `contains` operator is planned — see follow-up spec; until then, prefer the least-constrained value that still isolates the entity.)*
> - On **high-volume transaction tables**, always bound the time range and add the most specific entity filter you have, so you read a small, relevant slice — not the whole table. **Filter on the day-grain `trans_dt` by default**; the exact `txn_date_time` timestamp is available but should be used only when the question needs within-day precision (ordering, time-of-day, the exact moment). Don't over-constrain with a full timestamp — it's both unnecessary and a zero-record risk.
> - If a filtered query returns 0 rows, **state the filter you used** and treat it as a candidate mismatch in `data_gaps`, rather than reporting "the customer has no such transactions" as fact.

**(c) Surfacing transactions in the answer.**
> For transaction-level answers, **show the specific transactions** that support the finding. Put a compact **markdown table** (date/time, amount or score, approve/deny, reason) in your evidence so the synthesizer can render it. *(A richer Plots-panel table via `make_chart(kind="table")` is planned — see follow-up spec; for now the reliable path is a markdown table in evidence/findings.)*

### B2. `skills/domain/modeling.md` — own the transaction tables

- Extend `data_hints` from `[model_scores, score_drivers]` → `[model_scores, score_drivers, model_scores_transaction, score_drivers_transaction]`.
- Add a short subsection:
  > **Monthly vs transaction.** `model_scores` / `score_drivers` are monthly aggregates — use for score *trajectory* and *driver rotation over time*. `model_scores_transaction` / `score_drivers_transaction` are per-transaction — use to inspect the scores/drivers **at a specific transaction**, or to analyze **approve/deny** behavior (`appr_deny_cd`: 0=approved, 1=declined; `auto_decline_pos_deny_cd_s1` = decline reason, set only on declines). CDSS = `credit_loss_prob`, TSR = `tot_struct_risk_score` in both grains; quote the column name as always.
  > These transaction tables are large — **always filter by a narrow time window (use day-grain `trans_dt` by default; reserve `txn_date_time` for within-day precision) and any known entity** before querying.

### B3. `skills/workflow/synthesis.md` — tables in the final answer

The synthesis skill already permits markdown tables (lines ~81–104). Add one line so transaction-level answers reliably get one:
> When the question is **transaction-level** (about specific transactions / approve-deny decisions), include a **markdown table of the relevant transactions** (e.g. date/time, amount or key score, approve-deny, decline reason) — these answers should show the underlying rows, not just prose.

### B4. Instruction acceptance

- A transaction-level question (e.g. "show the declined transactions in March 2024 and why") routes the modeling specialist to `model_scores_transaction`, filters `appr_deny_cd = 1` within a `trans_dt` range, and the final answer contains a markdown table of those transactions with the decline reason.
- A trend question ("how has CDSS moved this year") still routes to the monthly `model_scores`.
- No specialist over-scans the transaction table unfiltered.

---

## 5. Deferred — Workstream C (filter robustness, follow-up spec)

Root-caused during diagnosis (file:line in `tools/data_tools.py`); fix in a separate spec:
- **P0 — case/whitespace-sensitive `eq`/`ne` on free-text** (`_coerce_pair:330`): casefold + strip string compares for eq/ne.
- **P0 — no `contains`/substring op** (`_FILTER_OPS:52-59`, `_apply_filter`): add `contains` (case-insensitive), document in tool args + skills.
- **P1 — over-eager `float()` coercion corrupts leading-zero IDs / `1e3` / `inf` / `nan`** (`_coerce_pair:319-323`): gate numeric branch behind a strict numeric regex.
- **P1 — `ne` silently drops null cells** (`_apply_filter:608-623`): count nulls as satisfying `ne`.
- **P2 — trailing-digit fuzzy column match mis-binds `score_1`→`score_2`** (`_normalize:65-70`, `_resolve_real_column:580-585`): refuse on collision, return literal (honest zero).
- **P3 — `MMM-YY` (`Jul-25`) / Excel-serial dates unparseable** (`_date_key`, `_MONTH_YEAR_RE:145`): add regex branches + parametrized tests (per CLAUDE.md date rule). Note: CLAUDE.md's claim that `2024-01` vs `2024-01-01 15:25:20` won't `eq`-match is **inaccurate for current code** — `_date_key` already collapses both to day; correct that note when this lands.

## 6. Deferred — Workstream D (`make_chart(kind="table")` render fix, follow-up spec)

- The table KP → `chart` SSE pipeline is **mechanically correct** (`server.py:576` keeps url-less table KPs; emission at `:1701-1731`). The bug is that tables are **never produced**: the auto-distiller only emits `trend`/`trend_dual`/`share` (`redacting_tool.py:617-665,819`), and the skills tell specialists *"you do NOT need to call `make_chart`"* (`data_query.md:46-52,152`), with no `kind="table"` worked example or `x_field`/`y_fields` contract (`data_viz.md:31`, `data_viz_tools.py:104`).
- Fix options: (a) teach the auto-distiller to emit `kind="table"` for ≤3-point series; **(b)** add an explicit `kind="table"` worked example + column contract to `data_viz.md` and soften the "don't call make_chart" blanket so the transaction-row use case can call it. (b) is the path for transaction rows (which are not auto-distilled series).
- **Out-of-repo caveat:** the live frontend's `chart` handler for `kind=="table"` cannot be verified in this repo; confirm/implement its `numbers`-array renderer when D is done.
- Until D lands, B relies on **markdown tables in the answer** (which work today).

---

## 7. Files touched (this spec, A+B only)

**New:**
- `config/data_profiles/model_scores_transaction.yaml`
- `config/data_profiles/score_drivers_transaction.yaml`

**Edited:**
- `skills/workflow/data_query.md` (routing + filter-rigor + surface-transactions sections)
- `skills/domain/modeling.md` (`data_hints` + monthly-vs-transaction note)
- `skills/workflow/synthesis.md` (one line: transaction answers include a row table)

**Memory:** add a `.claude/memory/` entry for transaction-vs-monthly routing + the two new tables, and index it in `.claude/memory/MEMORY.md`.

No code, gateway, catalog, or loader changes.
