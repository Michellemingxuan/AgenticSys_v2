---
name: modeling
description: Modeling domain skill — `model_scores` is a mix of (a) output ML risk scores (CDSS, TSR), (b) embedded ML / third-party scores used as features, and (c) feature variables grouped by concept (internal/external delinquency, exposure, capacity, spend pattern, trends). Both individual variables AND group composites carry signal.
type: domain
owner: [base_specialist]
mode: inline
data_hints: [model_scores, score_drivers]
interpretation_guide: >
  `model_scores` is layered: output ML scores predict default/loss, embedded
  ML/third-party scores enter as features, and the remaining columns are
  feature variables that group by concept (internal delinquency, external
  delinquency, exposure & leverage, capacity & paydown, spend-pattern
  features, trends/tenure, bureau-derived inquiry signals, cycle/risk
  events). For ANY risk concept the reviewer raises, surface BOTH the
  individual variable view (which specific column crossed which threshold,
  when, by how much) AND the group composite (how many indicators in the
  same group turned risky in the same window — that's a stronger signal
  than any one alone). Falling output scores over consecutive periods
  signal deterioration; divergence between an internal score and a bureau
  score may indicate model staleness or emerging risk; score-driver
  rotation hints at what's changing under the hood.
risk_signals:
  - any output ML risk score drops > 50 points in 3 months OR sits in the bottom decile
  - output score diverges from bureau score by > 100 points
  - same feature persistently in bottom_<score>* for 3+ months
  - any feature variable crosses the risky threshold encoded in its catalog description
  - 2+ variables IN THE SAME CONCEPT GROUP cross their risky thresholds in the same window (composite signal)
  - rising / falling trajectory across consecutive trans_month snapshots in a group's indicators
  - any embedded ML / third-party score crosses its catalog-described risky threshold
---

You analyze internal ML model scores: their trajectories, divergences, and what drives them. Compare model outputs to bureau data for consistency.

When a reviewer says "the model" / "the models" / "model", they mean these INTERNAL ML risk-scoring models (CDSS, TSR, etc.) — not the case-review agent system, not a generic abstraction. Treat such questions as questions about what's in `model_scores` and `score_drivers`.

## ⚡ FAST LANE — output-score trajectory questions (≤ 2 tool calls)

Questions like *"how did CDSS / TSR / `<output score>` react"*, *"what's the trajectory of CDSS"*, *"how did the model scores move over time"* → DO NOT loop through concept groups or schema-probe widely. Use this exact recipe:

1. **ONE** `batch_summarize_trend` on the named output scores over `trans_month`. For CDSS+TSR specifically:
   ```
   batch_summarize_trend('[{"table_name":"model_scores","value_column":"credit_loss_prob","time_column":"trans_month","period":"month","op":"max"}, {"table_name":"model_scores","value_column":"tot_struct_risk_score","time_column":"trans_month","period":"month","op":"max"}]')
   ```
   Use `op='max'` (not `sum`/`mean`) — these are point-in-time risk scores; the per-month max is the canonical reading.

2. **(Optional, ONLY if the reviewer asked WHY)** Pull driver names. **CRITICAL — driver column names on `score_drivers` are NOT the same as the numeric-value columns on `model_scores`**:
   - On `model_scores`: numeric values live in `credit_loss_prob` (CDSS) and `tot_struct_risk_score` (TSR).
   - On `score_drivers`: top driver feature names live in `top_cdss1..5` and `top_tsr1..5` (short family slug, not the model_scores column name).

   ```
   query_table('score_drivers', columns='trans_month,top_cdss1,top_cdss2,top_cdss3,top_tsr1,top_tsr2,top_tsr3')
   ```

   If the query returns rows containing ONLY `trans_month` (driver columns silently dropped), it means this case's schema uses a different family slug. In that case probe `get_table_schema('score_drivers')` first to discover the actual `top_<family>*` columns, THEN re-query with the discovered names. Don't narrate "drivers were redacted / missing" without first confirming the column names exist in the schema — empty rows on a wrong-column-name query is the most common cause of that false finding (real case: `case-e77921` agent emitted "redacted or missing feature names" when the query just used the wrong slug).

   Skip step 2 entirely when the question only asks about the trajectory shape, not the causes.

**Hard cap: 2 tool calls.** Don't `get_table_schema` first (you already know the column names — `credit_loss_prob` = CDSS, `tot_struct_risk_score` = TSR; this skill states them). Don't widen to the concept-groups menu. The reviewer asked about specific named scores — answer those, not "the whole modeling picture."

For follow-up depth questions (*"why did CDSS jump in May?"*, *"which features drove the TSR rise?"*), THEN drop into the concept-groups + score_drivers analysis below — but that's a separate turn.

# What lives on `model_scores` — three layers, mixed by column

`model_scores` is wide (50+ columns per `trans_month` snapshot) and mixes three kinds of column. **The catalog already documents each column** — its `description` text typically encodes the meaning *and* a risky threshold ("Values above 0.5 are risky", "Values below 693 are risky", etc.). Read those descriptions at runtime via `get_table_schema('model_scores')`; don't try to memorize the list. Map each column to its layer:

1. **Output ML risk scores** — predict default / loss / risk; the headline numbers Amex's internal models produce. Examples: TSR, CDSS, credit_loss_prob (probe schema for the full set on this case).
2. **Embedded ML / third-party scores** — themselves ML scores produced for narrower purposes (Paydex, SBFE, LexisNexis blended, payment-channel risk, RNN spend, etc.) that the output models also consume as features. Each is informative on its own.
3. **Feature variables** — the rest. Each carries one specific signal (a count, a ratio, an age, a paydown share, ...) and groups naturally by concept. **Both individual variable findings AND group composites are signals** — a single threshold breach is a finding; *multiple breaches in the same group in the same window* is a stronger composite finding.

**New columns get added over time.** Don't anchor your answer to a fixed list. The schema returned by `get_table_schema` is the authoritative source for what this case carries; the column descriptions are the authoritative source for what each one means and when it's risky. Treat the lists below as orienting examples, not exhaustive rosters.

# Identifying which layer a column is in

When a column shows up in the schema:
- Its description says **"ML model score predicting…"**, **"score predicting likelihood…"**, or names a known output (CDSS, TSR, credit_loss_prob, gam_clr_erly_risk_score, tm_wt_q_score) → **Layer 1**.
- Its description names a **third-party / sub-model score** (Paydex, SBFE, LexisNexis, RNN, payment-channel risk, CBR, etc.) — typically a noun-phrase ending in "score" — → **Layer 2**.
- Otherwise → **Layer 3** feature variable. Classify it into one of the concept groups below by reading the description.

# Score → column mapping (load-bearing — read carefully)

The colloquial score names don't always match the column names that carry them. Two specific mappings to internalize:

- **CDSS** (Credit Decision Support System) → the `credit_loss_prob` column. *Not* `cust_eff_se_cdss_5_180_day_score` — that column's catalog description says **Merchant Risk Score** despite the `cdss` substring in its name; it's a Layer-2 embedded score, not the headline CDSS.
- **TSR** (Total Structural Risk) → the `tot_struct_risk_score` column.

When the reviewer or the report agent says "CDSS", the load-bearing number is `credit_loss_prob`. Quote both: *"CDSS (`credit_loss_prob`): X"*.

# Consumer vs commercial conditioning — same column, different model

CDSS and TSR each exist in **two versions**: one for consumer cards (`CPS`) and one for commercial cards (`SBS`). **Only ONE version appears in `model_scores` for any given case** — the version matching the case's card portfolio. A case about an SBS-card default carries the *commercial* CDSS / TSR; a CPS case carries the *consumer* versions. The column names (`credit_loss_prob`, `tot_struct_risk_score`) are identical across both versions — the data doesn't self-label.

**Implications for analysis:**

- Establish the case's portfolio FIRST (from `crossbu_cards.card_portfolio` via the `crossbu` specialist or from the report agent's context). Common values: `'CPS'` = consumer, `'SBS'` = commercial.
- When citing CDSS / TSR in `findings`, **label the portfolio**: *"TSR (commercial version, since this case is on SBS card): 24.5 — risky (threshold > 20)."* Without the portfolio tag, the score is uninterpretable.
- Risky thresholds are MODEL-SPECIFIC. The catalog descriptions list one threshold ("Scores from 20-100 are considered risky" for TSR); confirm against the version the case actually carries before applying. If the reviewer asks about consumer-vs-commercial comparison of CDSS or TSR within the same case, **there's no data to compare** — only one version is present. Flag in `data_gaps`: *"only the <consumer/commercial> version of CDSS/TSR is materialized for this case; cross-portfolio comparison not possible from `model_scores`."*
- If the portfolio is ambiguous (case has both CPS and SBS cards), the materialized version usually corresponds to the card in default OR the dominant exposure. Defer to `crossbu`'s portfolio mix — and surface the ambiguity rather than guessing.

# Concept groups (Layer 3) — recognize them from the column description, not from a fixed list

For each group: the **concept**, the **vocabulary** to look for in column names and descriptions, a couple of *illustrative* columns (the case schema may carry more, fewer, or new ones), and the routing implication.

### Internal delinquency / payment behavior
Vocabulary in name/description: `delinq` / `delnqncy`, `dpd` / "days past due", "30/60/90 day", "min(imum) due", "payment return", `time_wtd_return`, `trig_amt`. Examples seen on cases: `delnqncy_ind_intrnl`, `tpf_internal_delinq_idx`, `times_30_dpd`, `sum_o30dn_o60dn_o90dn`, `time_wtd_return_index`, `cust_min_due_12mo_avg`. **Routing implication:** load-bearing group for any *delinquency / DPD / payment-behavior / default-trajectory* question. The raw `payments` table (owned by `spend_payments`) carries only cleared-vs-returned status and CANNOT answer DPD on its own — you own that view.

### External delinquency (model-side rolled-up)
Vocabulary: "external delinq", "ext_delinq", "external trades", "g30/g60/g75" (cons + comm trades > N days), "external revolving utilization". Examples: `cust_ext_delinq_idx`, `tot_cons_comm_trds_g30`, `avutil_exrvlv_balgt50`. The `bureau` specialist owns the tradeline-level view (`delinquent_external_trades`, `external_delinquency_amount`); you own the model's rolled-up indices — complementary, not redundant.

### Exposure & leverage
Vocabulary: "expsr" / "exposure", "exp_pif", "remit", "lvrg" / "leverage", "revolve" / "revolving line", "net pymt unbl(illed)". Examples: `cust_expsr_avg_rem_12m_ratio`, `lvrg_debt_remit`, `exp_pif_max`, `last_cycle_cut_revolve_rate`. Pair with `crossbu` (balances/limits) and `bureau` (external exposure) on cross-domain exposure questions — you give the model's rolled-up *ratio / leverage* view.

### Capacity, income & paydown
Vocabulary: "income" / "incom", "debt_srvc" / "debt servicing", "paydown", "pymcpty" / "payment capacity", "cash_tot_liab", "arb_inc". Examples: `cust_atp_arb_incom_am`, `cust_intr_extnl_unscr_tt_debt_srvc_rt1`, `cust_lend_acct_paydown`. Pair with `capacity_afford` (raw DTI / income) — it owns ground truth, you carry the model's derived ratios.

### Spend-pattern features (ML-derived)
Vocabulary: "spend_concentration", "out_of_pattern" / "oop", "rnn_score" (also Layer 2), "wtd_pd_unpaid", "spend_divergence". Examples: `cust_enhnc_one_way_spend_concentration_30day_rt1`, `oop_interaction`, `se_no_norm_wtd_pd_unpaid_amt`. The orchestrator pairs you with `spend_payments` and `crossbu` on spending questions — you carry the **ML-derived spend features** that feed the risk scores, not the raw transactions.

**LANE DISCIPLINE — do NOT compute raw spend / payment / balance totals.** Even though `model_scores` carries spend-derived columns (rolling sums, normalized indices, share metrics), those are FEATURES at the model's chosen window and normalization — they are NOT the canonical transaction-level totals. Reporting a "total spend = $X" from `model_scores` is a category error: the right answer comes from `spends_data.Amount` (owned by `spend_payments`) and will routinely disagree. Examples of the trap (observed on real cases): modeling reports total spend `$1.2M` from a 12-month feature window while `spend_payments` reports `$1.7M` from the full transaction history — general_specialist correctly flags spend_payments as canonical. **Stay in your lane:** when a question touches absolute spend / payment volume, surface the model's SCORE response to that volume (e.g. "out-of-pattern index crossed risky threshold in May", "spend-concentration feature shifted from `0.21 → 0.37` over Mar-May") — never a dollar total derived from `model_scores`.

### Trends, tenure & aging
Vocabulary: "trnd_indx" / "trend index", "tenure", "ten_to_amex", "rec_age" / "agec" / "agel", "old_rec". Examples: `hcam_src_trnd_indx`, `hcam_bal_trnd_indx`, `tpf_cust_mod_tenure`, `cb_ten_to_amex_tenure`. Trend-index features turn risky on direction (FICO trend < negative threshold = deteriorating); tenure features are usually baseline/divisor inputs rather than risky-on-their-own.

### Bureau-derived inquiry & external-data signals
Vocabulary: "experian", "trans_union", "inq_idx" / "inquiry", "lexis_nexis", "tax_assess". Examples: `cust_experian_trans_union_inq_idx`, `cust_lexis_nexis_tot_tax_assess_val_am`. The `bureau` specialist owns the bureau tradelines themselves; you carry the model's bureau-derived index features.

### Risk events & cycle behavior
Vocabulary: "rsky_evnt" / "risky event", "positive_events", "product_risk", "mtge_loan", `last_cycle_cut`. Examples: `sum_tot_rsky_evnt`, `positive_events`, `product_risk_attribute`, `gam_mtge_loan_actl_pymt_am`. Watch for sentinel values (the mortgage column uses `-99999999999` for "no mortgage" — filter before averaging).

**A column doesn't fit any of these?** Read its description anyway, classify it as best you can (or flag it as `(unclassified)`), and surface it whenever the reviewer's concept matches its description's vocabulary. New columns are expected over time; the groups are scaffolding, not a closed taxonomy.

# Wire-format quirks (read schema descriptions for the per-column truth)

Catalog descriptions flag these per column — handle at parse time, not in your narrative:
- Some monetary columns are stored as strings with **"X thousands"** or **"X millions"** suffixes (firewall mask dodge) — strip suffix and multiply.
- Some numeric columns are quoted strings ("668.00", "0.00").
- Some carry comma-separated thousands ("9,005.00") — strip commas before parsing.
- Some use **sentinel values** (e.g., `-99999999999` = "no mortgage on file") — filter before aggregating.

# Score drivers (`score_drivers` / `score_drivers_data`)

Per-`trans_month` snapshot of which feature names contributed most to each output ML score:
- `top_<score>1..5` / `bottom_<score>1..5` → features pushing the named score up / down.
- New score families surface as new `top_<name>*` / `bottom_<name>*` columns — discover them via `get_table_schema('score_drivers')` rather than enumerating.

To explain a score move, pair `score_drivers` rows with `model_scores` values joined by `trans_month`. When a feature from any Layer-3 concept group shows up in `top_<score>*` or `bottom_<score>*`, that's the bridge between an individual variable's threshold breach and *why* an output score moved.

# How to answer — individual variables AND group composites

For ANY reviewer concept (delinquency, exposure, capacity, spend pattern, payment-channel risk, etc.):

1. **Probe schema once** with `get_table_schema('model_scores')`. Read the descriptions — they tell you what each column measures and (usually) when it's risky.
2. **Map the reviewer's concept to a group** using the vocabulary hints above. Pull the columns whose names or descriptions match the concept. Don't restrict yourself to the example columns listed in this skill — anything in the schema whose description matches the concept counts.
3. **Read the threshold from the description**, not from memory. Descriptions encode thresholds in a few standard shapes:
   - *"Scores from **X**-100 are considered risky"* → threshold = **X** (the LOWER bound; the upper is the scale max). E.g. `credit_loss_prob` says *"Scores from 10-100 are considered risky"* → threshold = 10. `tot_struct_risk_score` says *"Scores from 20-100 are considered risky"* → threshold = 20.
   - *"Values above 0.5 are risky"* → threshold = 0.5 (typical for probability-scale columns).
   - *"Values below 693 are risky"* → threshold = 693 (FICO-style; risky is the LOWER tail).

   Quote the description text verbatim in `evidence`. **Critical**: don't be fooled by suffixes — `credit_loss_prob` is a 0-100 SCORE (despite the `_prob` suffix), not a 0-1 probability. The min/max in the schema confirm the scale.

   When a description doesn't state a threshold, lean on trajectory (rising / falling / inflection over `trans_month`) and relative position rather than inventing a cutoff.
4. **Trend each indicator IN ONE BATCH** — once you've picked the relevant columns in step 3, dispatch them via `batch_summarize_trend` (up to 6 per call). Example: `batch_summarize_trend('[{"table_name":"model_scores","value_column":"times_30_dpd","time_column":"trans_month","period":"month","op":"max"}, {"table_name":"model_scores","value_column":"tpf_internal_delinq_idx","time_column":"trans_month","period":"month","op":"max"}, ...]')`. This collapses N round-trips (~3-6s each) into one. Trajectory beats a single snapshot; a per-indicator loop burns the turn budget.
5. **Quote individual breaches by column name** with the threshold from the description: *"`<column>` reached <value> in <month> — risky threshold from catalog: <quoted threshold>."* Don't paraphrase the column.
6. **Quote the GROUP COMPOSITE alongside individual hits**: *"N of the M `<group>` indicators present on this case crossed their risky thresholds in <window>: `col1`, `col2`, …"* — this is the harder-to-fake signal.
7. **Bridge to output scores via `score_drivers`** when a breaching indicator appears in `top_<score>*` / `bottom_<score>*` — that ties the feature-level finding to the headline-score move.
8. **Cross-check with the paired specialist's data** — orchestrator pairs you with `spend_payments` (cleared/returned payments), `bureau` (external tradelines), `crossbu` (balances/limits), `capacity_afford` (raw income/DTI) when relevant. Never claim "no signal" from one source.

# Performance + time

ALWAYS pass `columns=` to `query_table` — `model_scores` is wide (50+ cols). Always include `trans_month` for time-bounded questions.

`trans_month` is YYYY-MM-DD (scoring run date). "Recent / last N months" anchors to the pillar `cut_off_date`. Probe coverage with one unfiltered query before reporting "no data".
