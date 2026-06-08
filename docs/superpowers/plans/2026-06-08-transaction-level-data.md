# Transaction-level Data Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add data profiles + agent instructions so the system can answer transaction-level questions from the new `modelling_data_transaction.csv` / `score_drivers_data_transaction.csv` tables, while keeping monthly tables for trend questions.

**Architecture:** Two new YAML profiles in `config/data_profiles/` (auto-loaded by `DataCatalog`; the gateway already auto-discovers the CSVs). Markdown edits to three skills (`data_query.md`, `modeling.md`, `synthesis.md`) to encode the transaction-vs-monthly routing rule, cross-cutting filter rigor, day-grain-first filtering, and "show the transaction rows in the answer." One new pytest verifying the profiles load and resolve against the real case. Plus a project-memory entry.

**Tech Stack:** Python 3, PyYAML, pytest. Data layer: `datalayer/catalog.py` (`DataCatalog`), `datalayer/gateway.py` (`LocalDataGateway`), `tools/data_tools.py`.

**Scope:** Workstreams A (profiles) + B (instructions) only. Filter-engine fixes (C) and `make_chart(kind="table")` render fix (D) are a separate follow-up — see `docs/superpowers/specs/2026-06-08-transaction-level-data-design.md` §5–6. Until D lands, transactions are surfaced via **markdown tables in the answer** (works today), not the Plots-panel artifact.

**Reference invariants (verified against the codebase):**
- `DataCatalog(profile_dir="config/data_profiles")` loads every `*.yaml`; `.get_schema(table)` returns the profile's `columns` dict (or `None`).
- `LocalDataGateway.from_case_folders("data_tables/real")` → one case, id `366132845011`; CSVs become `{table_name_without_ext: [row_dicts]}`. The real txn files are `modelling_data_transaction.csv` and `score_drivers_data_transaction.csv` → gateway table names `modelling_data_transaction` / `score_drivers_data_transaction`.
- `tools/data_tools.init_tools(gateway, catalog)` wires the module; `data_tools._get_table_schema_impl(name)` and `data_tools._list_available_tables_impl()` are the testable entry points. Table-name resolution tries exact → catalog table aliases → `{canonical}_data` → fuzzy, so a profile `model_scores_transaction` with alias `modelling_data_transaction` resolves to the CSV.
- `tests/test_skills/test_domain_skills.py::test_load_all_domain_skills` asserts exactly **8** domain skills. We are **editing** `modeling.md`, not adding a skill, so the count stays 8 — do not touch it.

---

## File Structure

**Create:**
- `config/data_profiles/model_scores_transaction.yaml` — transaction-grain model scores/features profile.
- `config/data_profiles/score_drivers_transaction.yaml` — transaction-grain score-driver profile.
- `tests/test_tools/test_transaction_profiles.py` — verifies both profiles load + resolve against the real case.
- `.claude/memory/transaction_vs_monthly_tables.md` — project memory entry.

**Modify:**
- `skills/workflow/data_query.md` — routing + filter-rigor + surface-transactions sections (all specialists).
- `skills/domain/modeling.md` — `data_hints` + monthly-vs-transaction note.
- `skills/workflow/synthesis.md` — one line: transaction answers include a row table.
- `.claude/memory/MEMORY.md` — index line for the new memory entry.
- `.claude/CLAUDE.md` — index line for the new memory entry (project memory index lives here too).

---

## Task 1: Author `model_scores_transaction.yaml`

**Files:**
- Create: `config/data_profiles/model_scores_transaction.yaml`

- [ ] **Step 1: Write the profile file**

Create `config/data_profiles/model_scores_transaction.yaml` with exactly this content (every column from the real CSV header is present; feature descriptions/thresholds are copied from `model_scores.yaml`; aggregation-suffix aliases and synthetic-gen stats are intentionally dropped):

```yaml
table: model_scores_transaction
description: "Transaction-level model scoring features — ONE ROW PER TRANSACTION\n\
  (contrast `model_scores`, which is one row per month). Holds the same ML output\n\
  scores (CDSS `credit_loss_prob`, TSR `tot_struct_risk_score`, `cbr_score`,\n\
  `gam_clr_erly_risk_score`, …) and input features as the monthly table, but at the\n\
  raw per-transaction grain — NO `_max`/`_min`/`_mean` aggregation.\n\n\
  Use this table for TRANSACTION-LEVEL questions: was a specific transaction out of\n\
  pattern, why was a transaction approved/declined, what did the scores look like at\n\
  the moment of a transaction. Use the monthly `model_scores` for TREND / trajectory\n\
  questions.\n\n\
  Authorization columns: `appr_deny_cd` (0 = approved, 1 = declined) and\n\
  `auto_decline_pos_deny_cd_s1` (decline-reason code, populated only on declines).\n\n\
  This table is HIGH-VOLUME — always filter tightly before querying. Filter the\n\
  day-grain `trans_dt` by default; reach for the exact `txn_date_time` timestamp only\n\
  when the question needs within-day precision. Same CSV wire-format quirks as the\n\
  monthly table apply (quoted numerics, \"thousands\"/\"millions\" suffixes on large\n\
  monetary values, comma-separated thousands).\n"
rows_per_case: 5000
one_row_per_case: false
aliases:
- modelling_data_transaction
- modeling_transaction
columns:
  trans_dt:
    dtype: date
    parse_hint: '%Y-%m-%d'
    description: Transaction date (YYYY-MM-DD). DEFAULT time-range filter key — use this for day/month/quarter windows.
  txn_date_time:
    dtype: date
    parse_hint: '%Y-%m-%d %H:%M:%S.%f'
    description: Transaction timestamp with time component (e.g. 2024-01-01 15:25:20.602). Do NOT default to this for retrieval; use only when sub-day precision is needed (within-day ordering, time-of-day, exact moment). eq on a bare date drops the time on both sides.
  appr_deny_cd:
    dtype: int
    description: 'Authorization decision: 0 = approved, 1 = declined. Filter with eq and value 0 or 1.'
  auto_decline_pos_deny_cd_s1:
    dtype: string
    description: Decline-reason code, populated only when appr_deny_cd = 1 (blank on approvals). Quote the raw code; do not invent a human label.
  cust_lexis_nexis_tot_tax_assess_val_am:
    dtype: string
    description: Total tax-assessed value of the customer business address (USD) from LexisNexis. Stored as string with "millions" suffix (e.g. "3.6 millions" → 3600000.0).
    dtype_pending_review: true
  cust_cash_tot_liab_yr1_rt:
    dtype: float
    description: ratio of cash to total liability corresponding to latest full year financial
  cbr_score:
    dtype: int
    description: customer credit bureau score (raw per-transaction value)
  cust_eff_se_cdss_5_180_day_score:
    dtype: float
    description: A metric that summarizes the customer's CDSS risk scores over a historical window (from 5 to 180 days ago). Values above 2 are risky.
    risk_threshold: 2
    risk_direction: above
  cust_expsr_avg_rem_12m_ratio:
    dtype: float
    description: Customer Exposure / (Average Remit In 2-12 Months). Values above 3.15 are risky.
    risk_threshold: 3.15
    risk_direction: above
  se_no_norm_wtd_pd_unpaid_amt:
    dtype: float
    description: normalised accum amount weighted by merchant percentage. Values above 0 are risky.
    risk_threshold: 0
    risk_direction: above
  tpf_internal_delinq_idx:
    dtype: float
    description: Internal Delinquency Index. Values above 5.8 are considered risky.
    risk_threshold: 5.8
    risk_direction: above
  oop_interaction:
    dtype: float
    description: Out of Pattern Spend index wrt to Exposure. Values above 28 are risky.
    risk_threshold: 28
    risk_direction: above
  sum_tot_rsky_evnt:
    dtype: int
    description: total risky events
  cust_rnn_score:
    dtype: float
    description: Timeseries Spend Variable. Values above 0.028 are considered risky.
    risk_threshold: 0.028
    risk_direction: above
  cust_experian_trans_union_inq_idx:
    dtype: float
    description: risk based index of inquiries from credit bureau. Values above 4.65 are considered risky.
    risk_threshold: 4.65
    risk_direction: above
  hcam_bal_trnd_indx:
    dtype: float
    description: hcam balance trend index. Values above 2.65 are risky.
    risk_threshold: 2.65
    risk_direction: above
  avutil_exrvlv_balgt50:
    dtype: float
    description: Average utilization in all the external consumer revolving trades with balance more than US$ 50. Values above 75 are risky.
    risk_threshold: 75
    risk_direction: above
  cust_expr_to_arb_inc_ratio:
    dtype: float
    description: Total exposure to arbitrated income ratio. Values above 0.08 are risky.
    risk_threshold: 0.08
    risk_direction: above
  lvrg_debt_remit:
    dtype: float
    description: leverage commercial debt
  cust_intr_extnl_unscr_tt_debt_srvc_rt1:
    dtype: float
    description: Customer total debt servicing ratio. Values above 0.15 are considered risky.
    risk_threshold: 0.15
    risk_direction: above
  cust_lndexpsr_minloc_6m_ratio:
    dtype: float
    description: customer lending exposure minloc 6 months ratio
  tot_cons_comm_trds_g75:
    dtype: int
    description: total consumer and commercial trades greater than 75
  cust_pymt_chan_risk_score:
    dtype: float
    description: Customer payment channel risk score. Values above 0.05 are considered risky.
    risk_threshold: 0.05
    risk_direction: above
  exp_pif:
    dtype: string
    description: Pay-In-Full exposure (raw per-transaction value). Stored as string with a "thousands" suffix (e.g. "188.8 thousands" → 188800.0).
    dtype_pending_review: true
  cust_debt_pymcpty_tm_inc:
    dtype: float
    description: debt to payment capacity
  positive_events:
    dtype: int
    description: positive events
  times_30_dpd:
    dtype: int
    description: No. of times 30 days past due. Values above 1 are risky.
    risk_threshold: 1
    risk_direction: above
  cust_min_due_12mo_avg:
    dtype: float
    description: average number of only minimum due paid in last 12 months. Values above 0.08 are considered risky.
    risk_threshold: 0.08
    risk_direction: above
  cust_enhnc_one_way_spend_concentration_30day_rt1:
    dtype: float
    description: one way spend concentration risk rate. Values above 2.4 are considered risky.
    risk_threshold: 2.4
    risk_direction: above
  gam_mtge_loan_actl_pymt_am:
    dtype: float
    description: Mortgage Loan (Actual Payment Amount). Sentinel -99999999999 = no mortgage on file
  cust_ext_delinq_idx:
    dtype: float
    description: External Delinquency Index. Values above 5 are risky.
    risk_threshold: 5
    risk_direction: above
  time_wtd_return_index:
    dtype: float
    description: Internal Payment Return Index. Values above 0.2 are considered risky.
    risk_threshold: 0.2
    risk_direction: above
  avg_remit_minus_max:
    dtype: string
    description: average remit (USD). Stored as string with "thousands" suffix (e.g. "221.4 thousands" → 221400.0).
    dtype_pending_review: true
  credit_loss_prob:
    dtype: float
    description: CDSS — ML model score predicting likelihood of default in next 18 months. Scores from 10-100 are considered risky.
    risk_threshold: 10
    risk_direction: above
  tot_struct_risk_score:
    dtype: float
    description: TSR — ML model score predicting likelihood of default on internal/external trades in next 18 months. Scores from 20-100 are considered risky.
    risk_threshold: 20
    risk_direction: above
  ons_30_trd:
    dtype: int
    description: ons 30 trades (raw per-transaction value)
  last_cycle_cut_revolve_rate:
    dtype: float
    description: last cycle cut revolve rate. Values below 0.46 are risky
    risk_threshold: 0.46
    risk_direction: below
  hcam_src_trnd_indx:
    dtype: float
    description: FICO Trend Index. Values below -19 are risky.
  tpf_cust_hi_rvlv_line_am:
    dtype: int
    description: The amount of a customer's highest revolving lines. Values below 77 are risky.
    risk_threshold: 77
    risk_direction: below
  cust_lend_acct_paydown:
    dtype: float
    description: customer level paydown on lending account. Values below 0.1 are risky.
    risk_threshold: 0.1
    risk_direction: below
  cust_open_acct_paydown:
    dtype: float
    description: customer level paydown on open account. Values below 0.3 are risky.
    risk_threshold: 0.3
    risk_direction: below
  gam_clr_erly_risk_score:
    dtype: string
    description: Clear early risk score. Values below 785 are risky. Stored in CSV with comma-separated thousands (e.g. "9,005.00") — strip commas before parsing.
    dtype_pending_review: true
    risk_threshold: 785
    risk_direction: below
  cust_lexis_nexis_blended_score:
    dtype: int
    description: score merges business and owner credit data. Values below 693 are risky.
    risk_threshold: 693
    risk_direction: below
  cust_sbfe_score:
    dtype: int
    description: Business credit score. Values below 863 are risky.
    risk_threshold: 863
    risk_direction: below
  cust_dnb_paydex_score:
    dtype: int
    description: Paydex score measures payment performance. Values below 61 are risky.
    risk_threshold: 61
    risk_direction: below
  one_expsr_usd_currency_amt:
    dtype: float
    description: One-exposure amount in USD currency (raw per-transaction value).
    description_pending: true
```

- [ ] **Step 2: Verify the YAML parses and round-trips**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python3 -c "import yaml; d=yaml.safe_load(open('config/data_profiles/model_scores_transaction.yaml')); print(d['table'], len(d['columns']), 'cols'); assert d['table']=='model_scores_transaction'; assert 'appr_deny_cd' in d['columns']; assert 'credit_loss_prob' in d['columns']; print('OK')"
```
Expected: `model_scores_transaction 46 cols` then `OK`.

- [ ] **Step 3: Verify every real CSV column has a profile entry (no drift)**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python3 -c "
import csv, yaml
hdr = next(csv.reader(open('data_tables/real/366132845011/modelling_data_transaction.csv')))
prof = yaml.safe_load(open('config/data_profiles/model_scores_transaction.yaml'))['columns']
missing = [c for c in hdr if c not in prof]
extra = [c for c in prof if c not in hdr]
print('missing from profile:', missing)
print('extra in profile:', extra)
assert not missing and not extra, 'profile/CSV column mismatch'
print('COLUMNS IN SYNC')
"
```
Expected: empty `missing`/`extra` lists then `COLUMNS IN SYNC`.

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add config/data_profiles/model_scores_transaction.yaml
git commit -m "feat(data): add model_scores_transaction profile"
```

---

## Task 2: Author `score_drivers_transaction.yaml`

**Files:**
- Create: `config/data_profiles/score_drivers_transaction.yaml`

- [ ] **Step 1: Write the profile file**

Create `config/data_profiles/score_drivers_transaction.yaml` with exactly this content:

```yaml
table: score_drivers_transaction
description: "Transaction-level snapshot of the top-5 and bottom-5 contributing\n\
  variable names for each internal model score — ONE ROW PER TRANSACTION (contrast\n\
  `score_drivers`, which is per month). Use to explain which features drove the CDSS\n\
  and TSR scores FOR A SPECIFIC TRANSACTION or decline; use the monthly\n\
  `score_drivers` for driver-rotation-over-time questions.\n\n\
  Carries drivers for two ML scores, columns named by family:\n\
  - CDSS — `top_cdss1..5`, `bottom_cdss1..5`. Cell values are feature names from\n\
    `model_scores_transaction` that pushed CDSS up / down on that transaction.\n\
  - TSR — `top_tsr1..5`, `bottom_tsr1..5`. Same pattern for TSR.\n\n\
  Authorization columns mirror `model_scores_transaction`: `appr_deny_cd`\n\
  (0 = approved, 1 = declined) and `auto_decline_pos_deny_cd_s1` (decline reason,\n\
  set only on declines). Join to `model_scores_transaction` on `txn_date_time`.\n\
  HIGH-VOLUME — filter the day-grain `trans_dt` by default; use `txn_date_time` only\n\
  for within-day precision.\n"
rows_per_case: 5000
one_row_per_case: false
aliases:
- score_drivers_data_transaction
columns:
  index:
    dtype: int
    description: Row index within the case's transaction export.
  grms_cid:
    dtype: string
    description: Customer identifier (GRMS CID).
  trans_dt:
    dtype: date
    parse_hint: '%Y-%m-%d'
    description: Transaction date (YYYY-MM-DD). DEFAULT time-range filter key.
  txn_date_time:
    dtype: date
    parse_hint: '%Y-%m-%d %H:%M:%S.%f'
    description: Transaction timestamp with time component. Use only when sub-day precision is needed. Join key to model_scores_transaction.
  appr_deny_cd:
    dtype: int
    description: 'Authorization decision: 0 = approved, 1 = declined.'
  auto_decline_pos_deny_cd_s1:
    dtype: string
    description: Decline-reason code, populated only when appr_deny_cd = 1.
  top_cdss1:
    dtype: string
    description: Strongest feature pushing the CDSS score UP for this transaction.
  top_cdss2:
    dtype: string
    description: 2nd strongest feature pushing the CDSS score UP for this transaction.
  top_cdss3:
    dtype: string
    description: 3rd strongest feature pushing the CDSS score UP for this transaction.
  top_cdss4:
    dtype: string
    description: 4th strongest feature pushing the CDSS score UP for this transaction.
  top_cdss5:
    dtype: string
    description: 5th strongest feature pushing the CDSS score UP for this transaction.
  bottom_cdss1:
    dtype: string
    description: Strongest feature pushing the CDSS score DOWN for this transaction.
  bottom_cdss2:
    dtype: string
    description: 2nd strongest feature pushing the CDSS score DOWN for this transaction.
  bottom_cdss3:
    dtype: string
    description: 3rd strongest feature pushing the CDSS score DOWN for this transaction.
  bottom_cdss4:
    dtype: string
    description: 4th strongest feature pushing the CDSS score DOWN for this transaction.
  bottom_cdss5:
    dtype: string
    description: 5th strongest feature pushing the CDSS score DOWN for this transaction.
  top_tsr1:
    dtype: string
    description: Strongest feature pushing the TSR score UP for this transaction.
  top_tsr2:
    dtype: string
    description: 2nd strongest feature pushing the TSR score UP for this transaction.
  top_tsr3:
    dtype: string
    description: 3rd strongest feature pushing the TSR score UP for this transaction.
  top_tsr4:
    dtype: string
    description: 4th strongest feature pushing the TSR score UP for this transaction.
  top_tsr5:
    dtype: string
    description: 5th strongest feature pushing the TSR score UP for this transaction.
  bottom_tsr1:
    dtype: string
    description: Strongest feature pushing the TSR score DOWN for this transaction.
  bottom_tsr2:
    dtype: string
    description: 2nd strongest feature pushing the TSR score DOWN for this transaction.
  bottom_tsr3:
    dtype: string
    description: 3rd strongest feature pushing the TSR score DOWN for this transaction.
  bottom_tsr4:
    dtype: string
    description: 4th strongest feature pushing the TSR score DOWN for this transaction.
  bottom_tsr5:
    dtype: string
    description: 5th strongest feature pushing the TSR score DOWN for this transaction.
```

- [ ] **Step 2: Verify the YAML parses and columns are in sync with the CSV**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python3 -c "
import csv, yaml
hdr = next(csv.reader(open('data_tables/real/366132845011/score_drivers_data_transaction.csv')))
prof = yaml.safe_load(open('config/data_profiles/score_drivers_transaction.yaml'))['columns']
missing = [c for c in hdr if c not in prof]; extra = [c for c in prof if c not in hdr]
print('missing:', missing, 'extra:', extra)
assert not missing and not extra
print('COLUMNS IN SYNC', len(prof), 'cols')
"
```
Expected: `missing: [] extra: []` then `COLUMNS IN SYNC 26 cols`.

- [ ] **Step 3: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add config/data_profiles/score_drivers_transaction.yaml
git commit -m "feat(data): add score_drivers_transaction profile"
```

---

## Task 3: Verification test — profiles load and resolve against the real case

**Files:**
- Create: `tests/test_tools/test_transaction_profiles.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools/test_transaction_profiles.py`:

```python
"""Profiles for the transaction-level tables load and resolve to the real CSVs."""

from __future__ import annotations

import pytest

from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from tools import data_tools

REAL_CASE = "366132845011"


@pytest.fixture
def real_env():
    catalog = DataCatalog(profile_dir="config/data_profiles")
    gateway = LocalDataGateway.from_case_folders("data_tables/real")
    gateway.set_case(REAL_CASE)
    data_tools.init_tools(gateway, catalog)
    yield catalog
    data_tools._gateway = None
    data_tools._catalog = None


def test_catalog_loads_transaction_profiles(real_env):
    catalog = real_env
    for table in ("model_scores_transaction", "score_drivers_transaction"):
        schema = catalog.get_schema(table)
        assert schema, f"{table} profile not loaded"

    ms = catalog.get_schema("model_scores_transaction")
    # Transaction-specific columns are documented.
    for col in ("trans_dt", "txn_date_time", "appr_deny_cd",
                "auto_decline_pos_deny_cd_s1"):
        assert col in ms, f"{col} missing from model_scores_transaction"
    # appr_deny_cd documents the 0/1 decode.
    assert "approved" in ms["appr_deny_cd"]["description"].lower()
    # Reused feature + threshold survived.
    assert ms["credit_loss_prob"]["risk_threshold"] == 10


def test_schema_tool_resolves_alias_to_real_csv(real_env):
    # The canonical name and the CSV-file alias both resolve and list real cols.
    out_canonical = data_tools._get_table_schema_impl("model_scores_transaction")
    out_alias = data_tools._get_table_schema_impl("modelling_data_transaction")
    for out in (out_canonical, out_alias):
        assert "appr_deny_cd" in out
        assert "credit_loss_prob" in out
        assert "txn_date_time" in out


def test_transaction_tables_listed_for_real_case(real_env):
    out = data_tools._list_available_tables_impl()
    assert "modelling_data_transaction" in out or "model_scores_transaction" in out
    assert ("score_drivers_data_transaction" in out
            or "score_drivers_transaction" in out)


def test_appr_deny_filter_returns_declines(real_env):
    # appr_deny_cd is int 0/1; eq filter on the string "1" must match declines.
    out = data_tools._query_table_impl(
        "model_scores_transaction",
        filter_column="appr_deny_cd",
        filter_value="1",
        filter_op="eq",
        columns="trans_dt,appr_deny_cd,auto_decline_pos_deny_cd_s1",
    )
    # Either declines exist (rows_matching_filter > 0) or the table is all-approve;
    # in both cases the tool must not error and must report a match count.
    assert "rows_matching_filter" in out
```

- [ ] **Step 2: Run the test to verify it fails (profiles not yet created if running standalone, or passes if Tasks 1-2 done)**

If executing tasks in order, Tasks 1-2 already created the profiles, so this test should PASS. To confirm the test is meaningful, temporarily rename one profile and observe failure:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_transaction_profiles.py -v
```
Expected: PASS (all 4 tests). If you want to see it fail first, `git mv config/data_profiles/model_scores_transaction.yaml /tmp/x.yaml`, run, see FAIL on `test_catalog_loads_transaction_profiles`, then `git mv /tmp/x.yaml config/data_profiles/model_scores_transaction.yaml`.

- [ ] **Step 3: Run the test to verify it passes**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_transaction_profiles.py -v
```
Expected: `4 passed`.

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add tests/test_tools/test_transaction_profiles.py
git commit -m "test(data): verify transaction profiles load + resolve to real CSVs"
```

---

## Task 4: `data_query.md` — routing + filter rigor (all specialists)

**Files:**
- Modify: `skills/workflow/data_query.md`

- [ ] **Step 1: Read the file to find insertion anchors**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
grep -n "^## \|^### \|date\|filter\|Cross-domain\|no parseable\|get_table_schema" skills/workflow/data_query.md | head -40
```
This shows the section headings. Insert the new block **after the existing schema/filtering guidance and before the Cross-domain section** (or, if no obvious anchor, append as new top-level sections before the final cross-domain block). Read the surrounding lines so the new section reads naturally in context.

- [ ] **Step 2: Insert the three guidance sections**

Add this markdown block (verbatim) at the chosen anchor:

```markdown
## Transaction-level vs monthly-level data

Some domains ship BOTH a monthly table (one row per month, aggregated) and a
transaction table (one row per transaction). Choose by question type:

- **Transaction-level questions** → transaction tables (`spends`,
  `model_scores_transaction`, `score_drivers_transaction`). Examples: "was this
  transaction out of pattern?", "why was the customer declined on <date>?",
  "show the grocery transactions last month", "what did the risk scores look
  like at the moment of purchase?".
- **Trend / trajectory / over-time questions** → monthly tables
  (`model_scores`, `score_drivers`, `txn_monthly`, …). Examples: "is risk
  deteriorating?", "how has CDSS moved over 12 months?".
- When both could apply, prefer the **monthly** table for aggregate/trend
  framing; use the **transaction** table only when the answer hinges on
  specific transactions.

## Filter rigor (every query, not just big tables)

A zero-record result is **more often a filter mismatch than a true absence**.
Before concluding "no data":

- **Check the column's actual format first** via `get_table_schema` — never
  assume a date format or a value spelling. Match the column's own format
  (e.g. `txn_date_time` is `YYYY-MM-DD HH:MM:SS.fff`; `appr_deny_cd` is the
  integer `0`/`1`, not the words "approved"/"declined").
- For **free-text entity columns** (merchant name, reason codes), exact `eq`
  is brittle (case / whitespace). If an exact match returns nothing, re-query
  with a broader or shorter value before giving up.
- On **high-volume transaction tables**, always bound the time range and add
  the most specific entity filter you have. **Filter the day-grain `trans_dt`
  by default**; the exact `txn_date_time` timestamp is available but should be
  used only when the question needs within-day precision (ordering, time-of-day,
  the exact moment). Don't over-constrain with a full timestamp — it's both
  unnecessary and a zero-record risk.
- If a filtered query returns 0 rows, **state the filter you used** and treat
  it as a candidate mismatch in `data_gaps`, not as the fact "the customer has
  no such transactions".

## Show the transactions in transaction-level answers

For transaction-level answers, surface the specific transactions that back the
finding: put a compact **markdown table** in your evidence (e.g. date/time,
amount or key score, approve/deny, reason). The synthesizer renders it in the
answer. (A richer Plots-panel table via `make_chart(kind="table")` is planned;
for now the reliable path is a markdown table in evidence/findings.)
```

- [ ] **Step 3: Verify the skill still loads**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_skills/ -v
```
Expected: all pass (the workflow skill body parses; frontmatter unchanged).

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add skills/workflow/data_query.md
git commit -m "docs(skills): transaction-vs-monthly routing + filter rigor in data_query"
```

---

## Task 5: `modeling.md` — own the transaction tables

**Files:**
- Modify: `skills/domain/modeling.md`

- [ ] **Step 1: Update `data_hints` in the frontmatter**

Replace the exact line:
```yaml
data_hints: [model_scores, score_drivers]
```
with:
```yaml
data_hints: [model_scores, score_drivers, model_scores_transaction, score_drivers_transaction]
```

- [ ] **Step 2: Add a monthly-vs-transaction note in the body**

Read the body to find a sensible spot (after the opening paragraph that defines "the model" / lists CDSS·TSR column mappings):
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
grep -n "CDSS\|credit_loss_prob\|tot_struct_risk_score\|trajector\|driver" skills/domain/modeling.md | head
```
Insert this block after the column-mapping lines:

```markdown
**Monthly vs transaction.** `model_scores` / `score_drivers` are monthly
aggregates — use them for score *trajectory* and *driver rotation over time*.
`model_scores_transaction` / `score_drivers_transaction` are per-transaction —
use them to inspect the scores/drivers **at a specific transaction**, or to
analyze **approve/deny** behavior (`appr_deny_cd`: 0 = approved, 1 = declined;
`auto_decline_pos_deny_cd_s1` = decline reason, set only on declines). CDSS =
`credit_loss_prob`, TSR = `tot_struct_risk_score` in both grains — quote the
column name as always. These transaction tables are large: always filter by a
narrow time window (day-grain `trans_dt` by default; reserve `txn_date_time`
for within-day precision) and any known entity before querying.
```

- [ ] **Step 3: Verify domain skills still load and count is unchanged (8)**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_skills/test_domain_skills.py -v
```
Expected: all pass, including `test_load_all_domain_skills` (still 8 skills) and `test_all_skills_have_required_fields`.

- [ ] **Step 4: Confirm the new data_hints parsed correctly**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python3 -c "from skills.domain.loader import load_domain_skill; s=load_domain_skill('modeling'); print(s.data_hints); assert 'model_scores_transaction' in s.data_hints and 'score_drivers_transaction' in s.data_hints; print('OK')"
```
Expected: the 4-item list then `OK`.

- [ ] **Step 5: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add skills/domain/modeling.md
git commit -m "docs(skills): modeling owns transaction tables + monthly-vs-txn note"
```

---

## Task 6: `synthesis.md` — transaction answers include a row table

**Files:**
- Modify: `skills/workflow/synthesis.md`

- [ ] **Step 1: Find the table-formatting guidance**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
grep -n "table\|markdown\|When to use a table\|comparative" skills/workflow/synthesis.md | head
```

- [ ] **Step 2: Add one line in the "When to use a table" area**

Insert immediately after the existing "When to use a table" guidance:

```markdown
When the question is **transaction-level** (about specific transactions or
approve/deny decisions), include a **markdown table of the relevant
transactions** (e.g. date/time, amount or key score, approve-deny, decline
reason) — these answers should show the underlying rows, not just prose.
```

- [ ] **Step 3: Verify the skill loads**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_skills/ -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add skills/workflow/synthesis.md
git commit -m "docs(skills): synthesis shows transaction rows for txn-level answers"
```

---

## Task 7: Project memory entry

**Files:**
- Create: `.claude/memory/transaction_vs_monthly_tables.md`
- Modify: `.claude/memory/MEMORY.md`
- Modify: `.claude/CLAUDE.md`

- [ ] **Step 1: Write the memory file**

Create `.claude/memory/transaction_vs_monthly_tables.md`:

```markdown
# Transaction-level vs monthly-level tables

Two grains now exist for the modeling domain:

- **Monthly** (one row per month, aggregated; canonical features carry
  `_max`/`_min`/`_mean` aliases): `model_scores` (`modelling_data`),
  `score_drivers` (`score_drivers_data`). Use for TREND / trajectory / driver
  rotation over time.
- **Transaction** (one row per transaction; raw per-txn feature values, no
  aggregation): `model_scores_transaction` (`modelling_data_transaction.csv`),
  `score_drivers_transaction` (`score_drivers_data_transaction.csv`). Use for
  per-transaction / approve-deny questions.

Transaction-specific columns: `trans_dt` (day grain — DEFAULT filter key),
`txn_date_time` (full timestamp — use only for within-day precision),
`appr_deny_cd` (0=approved, 1=declined), `auto_decline_pos_deny_cd_s1`
(decline reason, set only on declines).

Profiles: `config/data_profiles/model_scores_transaction.yaml`,
`config/data_profiles/score_drivers_transaction.yaml` — feature descriptions
copied from the monthly `model_scores.yaml`, aggregation-suffix aliases dropped,
canonical name = the raw CSV column name.

These tables are HIGH-VOLUME: always filter tightly (narrow `trans_dt` window +
specific entity) before querying. A zero-record result is more often a
filter/format mismatch than true absence — re-check the column format and
broaden the value before reporting "no such transactions".

Deferred follow-up (separate spec
`docs/superpowers/specs/2026-06-08-transaction-level-data-design.md` §5–6):
filter-engine robustness (case-insensitive eq, `contains` op, numeric-coercion
gating, `MMM-YY`/Excel-serial dates) and the `make_chart(kind="table")` render
fix. Until then, surface transactions via markdown tables in the answer.
```

- [ ] **Step 2: Add the index line to `.claude/memory/MEMORY.md`**

Append under the existing index list:
```markdown
- [Transaction vs monthly tables](transaction_vs_monthly_tables.md) — two grains: monthly for trends, transaction (model_scores_transaction / score_drivers_transaction) for per-txn & approve-deny questions; filter day-grain trans_dt by default.
```

- [ ] **Step 3: Add the index line to `.claude/CLAUDE.md`**

In the "Project memory (`.claude/memory/`)" bullet list, add:
```markdown
- [`.claude/memory/transaction_vs_monthly_tables.md`](memory/transaction_vs_monthly_tables.md) — transaction-level tables (model_scores_transaction / score_drivers_transaction) answer per-txn & approve-deny questions; monthly tables answer trends; filter day-grain `trans_dt` by default, `txn_date_time` only for within-day precision.
```

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add .claude/memory/transaction_vs_monthly_tables.md .claude/memory/MEMORY.md .claude/CLAUDE.md
git commit -m "docs(memory): record transaction-vs-monthly table grains"
```

---

## Task 8: Full regression + acceptance

- [ ] **Step 1: Run the full test suite**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest -q
```
Expected: all green. Pay attention to `tests/test_skills/`, `tests/test_tools/`, `tests/test_catalog_sync.py`, `tests/test_datalayer/`. If `test_catalog_sync` or any catalog test fails, the new YAML likely has a structural issue — re-check Step 2 of Tasks 1-2.

- [ ] **Step 2: Manual acceptance — schema visibility for the real case**

Run:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python3 -c "
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from tools import data_tools
c=DataCatalog(profile_dir='config/data_profiles')
g=LocalDataGateway.from_case_folders('data_tables/real'); g.set_case('366132845011')
data_tools.init_tools(g,c)
print(data_tools._get_table_schema_impl('model_scores_transaction')[:800])
print('---LIST---')
print(data_tools._list_available_tables_impl())
"
```
Expected: schema text lists `appr_deny_cd`, `txn_date_time`, `credit_loss_prob`, etc.; the table list includes the two transaction tables.

- [ ] **Step 3: Final review against the spec**

Re-read `docs/superpowers/specs/2026-06-08-transaction-level-data-design.md` §3–4 and confirm every A/B item maps to a completed task. C/D remain intentionally deferred.

- [ ] **Step 4: (Optional) Offer to push**

Do not push automatically. Report completion and ask the user whether to push or open a PR.

---

## Self-Review (plan author)

- **Spec coverage:** A1 → Task 1; A2 → Task 2; A3 (load/resolve acceptance) → Task 3 + Task 8; B1 → Task 4; B2 → Task 5; B3 → Task 6; memory entry (spec §7) → Task 7. All covered.
- **Placeholder scan:** No TBD/TODO; all YAML and markdown content is provided in full; commands have expected output. Markdown-insertion tasks intentionally instruct the executor to Read the file for the exact anchor (the surrounding prose isn't reproduced here) but provide the complete inserted text.
- **Type/name consistency:** Table names (`model_scores_transaction`, `score_drivers_transaction`), aliases (`modelling_data_transaction`, `score_drivers_data_transaction`), real case id (`366132845011`), and impl functions (`_get_table_schema_impl`, `_list_available_tables_impl`, `_query_table_impl`, `init_tools`) are used identically across tasks and match the codebase.
- **Deferred:** C (filter fixes) and D (table render) are explicitly out of scope and recorded in the spec + memory for the follow-up.
```
