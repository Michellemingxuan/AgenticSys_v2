# Catalog Reconciliation — Human TODO

**Created:** 2026-04-27
**Status:** Sync ran against `data_tables/real/366132845011`. Catalog now has 12 YAMLs. 24 columns + 1 table need descriptions. 5 profile-only tables + 26 profile-only columns need a keep/drop/mark decision. 2 aliases need a sanity-check.

## Context — what sync already did
- Read all real CSVs under `data_tables/real/366132845011/` (skipped 1 `.xlsx`).
- Folded every real column it could match into an existing canonical YAML (added 36+ aliases across `bureau`, `cross_bu`, `payments`, `spends`, `model_scores`).
- For real tables that didn't substring-match any canonical: created `modelling_data.yaml` (the only one — all others folded into existing canonicals).
- Wrote new columns under their resolved canonical with `description_pending: true` and a regex-pattern draft (no LLM was available — `OPENAI_API_KEY` not set).

What's left is everything that needed human judgment.

---

## 🔴 P1 — Write descriptions (24 columns + 1 table)

### Table description (1)
- [x] ~~**`modelling_data.yaml`** → set `description:` field.~~ **RESOLVED 2026-04-27** — confirmed `modelling_data.csv` is the same table as canonical `model_scores`. The 3 unique columns (`cbr_score_max`, `exp_pif_max`, `ons_30_trd_mean`) folded into `model_scores.yaml`; `modelling_data` added as a table-level alias on `model_scores`; orphan file deleted. (Code change: resolvers in `tools/data_tools.py`, `orchestrator/orchestrator.py`, and `datalayer/adapter.py` now honor table-level `aliases`.)

### Group A — `score_drivers` ranked driver names (20 cols, single pattern)
- [x] **RESOLVED 2026-04-27.** Template applied to all 20 cdss/tsr cols, `description_pending: false`. Also: added `trans_month` real column, dropped 6 simulator-only cols (`driver_rank`, `driver_variable`, `driver_direction`, `driver_value`, `driver_contribution`, `driver_description`), added table-level alias `score_drivers_data`. Side-effect: `score_drivers` is no longer simulatable (all cols now real-data shape) — generator skips it.

### Group B — Replace weak regex drafts (3 cols)
- [x] ~~**`spends.Date`**~~ **RESOLVED 2026-04-27** — was misrouted as a separate column; actually = canonical `spends.spend_date`. Aliased + added `parse_hint: "%d-%b-%Y"`. `spend_date` no longer profile-only.
- [x] ~~**`wcc_flags.Date`**~~ **RESOLVED 2026-04-27** — converged canonical `wcc_flags` to real `wcc` shape. See group below.
- [x] ~~**`model_scores.cbr_score_max`**~~ — RESOLVED 2026-04-27 — described as "customer credit bureau score (max)" by user edit.

### Group C — Genuinely empty (2 cols) — RESOLVED 2026-04-27
- [x] ~~**`model_scores.exp_pif_max`**~~ — described as Pay-In-Full exposure (max). **dtype changed string → float.** Stored as "X thousands" string to dodge the firewall's 6+digit mask; description spells out the parsing rule.
- [x] ~~**`model_scores.ons_30_trd_mean`**~~ — described as ONS 30 trades (mean). dtype kept `int` per user edit (semantic).

### Bonus discovered during model_scores sweep — RESOLVED 2026-04-27
- **`cust_lexis_nexis_tot_tax_assess_val_am`** — dtype int → float; "X millions" wire format documented.
- **`avg_remit_minus_max`** — dtype int → float; "X thousands" wire format documented.
- **`cust_net_pymt_unbl1`** — dtype already float; "X thousands" wire format documented.
- **`gam_clr_erly_risk_score`** — comma-separated thousands wire format ("9,005.00") documented.
- **Table-level description** updated with a CSV wire-format quirks note covering: (a) all numeric cols stored as quoted strings ("668.00" for ints), (b) "X thousands"/"X millions" suffixes on large monetary values, (c) comma-separated thousands. Saves 17+ per-column dtype-vs-storage notes.

### Group D — Empty col description, but col exists in real (1 col)
- [x] ~~**`wcc_flags.Note`**~~ **RESOLVED 2026-04-27** — see wcc converge below.

### Group E — wcc rename + reshape (RESOLVED 2026-04-27)
- Renamed `wcc_flags.yaml` → `wcc.yaml` and `table: wcc_flags` → `table: wcc`.
- Dropped simulator-only cols: `flag_type`, `severity`, `trigger_date`.
- Kept `Date` (was new) + `Note` (was new), both verified with semantic descriptions and `description_pending: false`.
- Updated domain skill `skills/domain/wcc.md`: `data_hints: [wcc]`, semantics rewritten from "watch-list flags" to "agent-call notes / customer-service log".
- Updated test fixture `tests/test_datalayer/test_generator.py` to allow renames (asserts concept survives, not literal name).
- Side-effect: `wcc` no longer simulatable (no distribution params on free-text); generator skips it.

---

## 🟡 P2 — Verify two suspect aliases

- [x] ~~**`bureau.avg_external_utilization` ← `Average External Utlization`**~~ **KEEP 2026-04-27** — the typo "Utlization" likely originates upstream in the data-preprocessing stage and may recur across other cases, so keeping it aliased is the right call.
- [x] ~~**`cross_bu.past_delinquencies_12m` ← `Past Delinquencies (Last 12M)`**~~ **KEEP 2026-04-27** — confirmed correct.

---

## 🟠 P3 — Keep / drop / mark profile-only entries — **RESOLVED 2026-04-27**

User's decision: **whole tables** not in real → keep YAML, exclude from case-filtered catalog (already automatic via `to_prompt_context(case_schema=...)`). **Columns** profile-only within tables that DO have real data → drop.

Resulting state:
- **Whole tables left intact** (4 sim-only): `cust_tenure`, `income_dti`, `txn_monthly`, `xbu_summary`. Verified: `describe_catalog()` for case 366132845011 does NOT render any of these.
- **Columns dropped**: 227 total across 4 canonicals.
  - `bureau`: 10 per-tradeline cols (was 28 → now 18). Real `bureau_data` is monthly aggregate.
  - `cross_bu`: 1 col (`merchant_industry`) — was 11 → now 10.
  - `model_scores`: 214 simulator-only cols — was 268 → now 54 (matches real `modelling_data.csv` shape).
  - `spends`: 2 cols (`month`, `customer_industry`) — was 10 → now 8.
- **Audit upgrade discovered**: previous `audit_profile_only` was row-based, missing columns from header-only CSV files (e.g., `crossbu_merchants_data.csv` has 0 rows in this case but valid headers). The cleanup pass used CSV `fieldnames` directly to be schema-level. Long-term fix: have `_build_observed` use headers, not `rows[0].keys()`.

### 5 tables (whole-table decisions)
- [ ] `cust_tenure`
- [ ] `income_dti`
- [x] ~~`model_scores`~~ — **NOT profile-only any more**. Resolved 2026-04-27 by aliasing real `modelling_data` to canonical `model_scores`.
- [ ] `txn_monthly`
- [ ] `xbu_summary`

### 26 columns (in tables that have real data, but these specific cols don't)

**`bureau` per-tradeline columns** — real data is monthly aggregate, not per-tradeline. These 10 cols are simulator-only by design:
- [ ] `score_date`, `trade_type`, `balance`, `credit_limit`, `utilization`, `dpd_status`, `delinquency_amount`, `is_revolving`, `open_date`, `last_reported_date`

**`cross_bu`** (3 cols, possibly in `crossbu_merchants_data` under different names):
- [ ] `merchant_name`, `merchant_industry`, `merchant_charge_volume`

**`score_drivers`** ~~(7 cols — older driver schema)~~ — **RESOLVED 2026-04-27**, all dropped from `score_drivers.yaml`.

**`spends`** ~~(3 cols)~~:
- [x] ~~`spend_date`~~ — RESOLVED, now aliased to real `Date`, no longer profile-only.
- [ ] `month`, `customer_industry` — still profile-only; decide.

**`wcc_flags`** ~~(3 cols)~~ — **all resolved by wcc rename + reshape.**

---

## How to execute

### Path 1 — Interactive sync with LLM (recommended for P1)
```bash
export OPENAI_API_KEY=sk-...
python -m datalayer.sync
```
Walks you through every `description_pending: true` column with a fresh LLM draft. ENTER = accept. `e` = edit. `r` = regenerate. About 24 prompts.

### Path 2 — Manual edits
Open each YAML in the IDE, fill in `description:`, flip `description_pending: false`. Best for the 20 `score_drivers` cols where the same template applies.

### Path 3 — Hybrid `verify` CLI (not yet built)
A small subcommand that finds pending columns and prompts for text only (no LLM needed). Ask Claude to build it if you want this — ~50 lines.

### For P2
Open `bureau.yaml` and `cross_bu.yaml`, find the two columns, decide. No code needed.

### For P3
Decide your overall stance, then I can apply it as one batch edit:
- **"Drop all simulator-only"** → I'll remove the 5 tables + 26 cols across YAMLs in one pass.
- **"Keep all"** → no action.
- **"Mark with `simulator_only: true`"** → adds the field + needs a follow-up code change to filter them out of agent-facing catalog views.

---

## Open Questions — ALL RESOLVED 2026-04-27

1. ~~**`model_scores` (canonical) vs `modelling_data` (real) — should they merge?**~~ **YES** — merged via table-level `aliases:` in YAML. Resolvers across the stack honor it.

2. ~~**`crossbu_cards_data` vs `crossbu_merchants_data` → fold into `cross_bu`?**~~ **NO — split.** Created `crossbu_cards.yaml` and `crossbu_merchants.yaml` as separate canonicals; deleted `cross_bu.yaml`. Updated `crossbu` domain skill `data_hints` and the test fixture.

3. ~~**`payments_success` + `payments_returns` → fold into `payments`?**~~ **YES — rbind.** Added a gateway hook (`LocalDataGateway._rbind_payments`) that concatenates them at load time and adds a synthetic `payment_status` column ("success" | "return"). Updated `payments.yaml` to declare `payment_status` + table-level aliases for the two source CSVs.

4. ~~**`demographics_data.xlsx` is invisible**~~ **RESOLVED** — user converted to CSV. Sync ingested it as a brand-new canonical `demographics_data.yaml` (16 cols). Discovered + fixed an Excel BOM issue along the way (CSV readers now use `utf-8-sig` encoding to auto-strip BOMs).

## Side fixes that came out of this round
- `_load_gateway` in sync.py now applies the same `_rbind_payments` post-load hook as the gateway proper.
- `_build_observed` now uses CSV headers (not row keys) when supplied — fixes the audit's blindness to header-only CSVs (e.g., empty `crossbu_merchants_data.csv` in this case).
- `audit_profile_only` now honors table-level aliases — `model_scores` no longer falsely flagged as profile-only just because it's accessed via the `modelling_data` alias.
- All CSV readers (gateway + sync) now use `utf-8-sig` encoding so Excel-exported BOM doesn't corrupt the first column header.

---

## When you're done
After P1+P2 are clean, verify:
```bash
python -c "
import yaml
from pathlib import Path
pending = []
for f in Path('config/data_profiles').glob('*.yaml'):
    p = yaml.safe_load(open(f))
    for col, spec in (p.get('columns') or {}).items():
        if isinstance(spec, dict) and spec.get('description_pending') is True:
            pending.append(f'{p[\"table\"]}.{col}')
print(f'{len(pending)} columns still pending:')
for c in pending: print(' -', c)
"
```
Target: zero pending.

Then commit:
```bash
git add config/data_profiles/
git commit -m "catalog: verify descriptions for real-data columns from case 366132845011"
```
