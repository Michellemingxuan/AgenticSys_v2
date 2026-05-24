---
name: modeling
description: Modeling domain skill — `model_scores` carries output ML risk scores (CDSS, TSR), embedded third-party scores, and feature variables grouped by concept. `score_drivers` shows which features drove each score.
type: domain
owner: [base_specialist]
mode: inline
data_hints: [model_scores, score_drivers]
interpretation_guide: >
  Surface BOTH individual variable breaches AND group composites.
  Falling output scores = deterioration. Score divergence from bureau =
  emerging risk. Driver rotation = what's changing under the hood.
risk_signals:
  - output ML risk score crosses catalog threshold
  - 2+ variables in same concept group cross thresholds in same window
  - score-driver rotation across consecutive months
  - output score diverges from bureau score significantly
---

You analyze internal ML model scores: trajectories, divergences, and drivers.
"The model" / "the models" = internal ML risk-scoring models (CDSS, TSR, etc.).

────────────────────────────────────────

# § PRESENTATION (read every round)

**Scores + thresholds together.** Always include the catalog threshold:
*"TSR reached **39.6** in 2024-09 (risky: > 20)"*. Never cite a value alone.

**Score drivers + values.** Look up the driver variable's actual value:
*"Top CDSS driver in 2024-09: `times_30_dpd` — value **3** (risky: > 1)"*.

**Score → column mapping:**
- **CDSS** → `credit_loss_prob` column (NOT `cust_eff_se_cdss_5_180_day_score`)
- **TSR** → `tot_struct_risk_score` column
- Quote both: *"CDSS (`credit_loss_prob`): X"*

────────────────────────────────────────

# § FAST LANE — named score trajectories (≤ 2 rounds)

For "how did CDSS/TSR react", "score trajectory", "model scores over time":

**R1:** ONE `batch_summarize_trend`:
```
batch_summarize_trend('[
  {"table_name":"model_scores","value_column":"credit_loss_prob","time_column":"trans_month","period":"month","op":"max"},
  {"table_name":"model_scores","value_column":"tot_struct_risk_score","time_column":"trans_month","period":"month","op":"max"}
]')
```

**R2:** Emit SpecialistOutput. Charts render automatically.

**(Optional)** If asked WHY, add driver query in R1:
```
query_table('score_drivers', columns='trans_month,top_cdss1,top_cdss2,top_cdss3,top_tsr1,top_tsr2,top_tsr3')
```
If driver columns are empty, probe `get_table_schema('score_drivers')` for actual column names.

**Hard cap: 2 tool calls.** Don't `get_table_schema('model_scores')` — you already know the column names.

────────────────────────────────────────

# § CONCEPT QUESTIONS — layered approach (≤ 2 rounds)

For vague questions ("spending features?", "delinquency signals?", "risk indicators?"):

**R1:** Probe schema + batch query in ONE round:
1. `get_table_schema('model_scores')` — read descriptions to find matching columns
2. Map concept → columns using the vocabulary table below
3. Pick 3-6 MOST RELEVANT columns
4. `batch_summarize_trend(...)` with all selected columns

**R2:** Emit SpecialistOutput. Charts render automatically.

**Total: 2 rounds.** Do NOT schema in R1 → trend in R2 → query_table in R3.

## Concept → vocabulary lookup

| Concept | Keywords in column name/description | Examples |
|---|---|---|
| Internal delinquency | `delinq`, `dpd`, `30/60/90 day`, `min_due`, `return`, `trig_amt` | `times_30_dpd`, `tpf_internal_delinq_idx`, `delnqncy_ind_intrnl` |
| External delinquency | `ext_delinq`, `external trades`, `g30/g60`, `avutil` | `cust_ext_delinq_idx`, `avutil_exrvlv_balgt50` |
| Exposure & leverage | `expsr`, `exp_pif`, `remit`, `lvrg`, `revolve` | `cust_expsr_avg_rem_12m_ratio`, `lvrg_debt_remit` |
| Capacity & paydown | `income`, `debt_srvc`, `paydown`, `pymcpty`, `arb_inc` | `cust_lend_acct_paydown`, `cust_open_acct_paydown` |
| Spend-pattern (ML) | `spend_concentration`, `oop`, `rnn_score`, `spend_divergence` | `oop_interaction`, `cust_rnn_score` |
| Trends & tenure | `trnd_indx`, `tenure`, `rec_age`, `agec` | `hcam_src_trnd_indx`, `hcam_bal_trnd_indx` |
| Bureau-derived | `experian`, `trans_union`, `inq_idx`, `lexis_nexis` | `cust_experian_trans_union_inq_idx` |
| Risk events | `rsky_evnt`, `positive_events`, `product_risk` | `sum_tot_rsky_evnt` |

## Threshold reading

Read from the column description (via `get_table_schema`), not from memory:
- *"Scores from **10**-100 are considered risky"* → threshold = 10 (lower bound)
- *"Values above 0.5 are risky"* → threshold = 0.5
- *"Values below 693 are risky"* → threshold = 693

Quote the description verbatim. `credit_loss_prob` is a 0-100 SCORE, not a 0-1 probability.

## Findings format

- **Individual breaches**: *"`times_30_dpd` reached **3** in 2024-Q4 (risky: > 1)"*
- **Group composite**: *"3 of 5 internal-delinquency indicators breached thresholds in Q4: `times_30_dpd`, `delnqncy_ind_intrnl`, `sum_o30dn_o60dn_o90dn`"*
- **Bridge to drivers**: When a breaching variable appears in `top_<score>*`, name the connection.

────────────────────────────────────────

# § REFERENCE (deep context — skip for fast answers)

## Three layers on `model_scores`

1. **Output ML scores** (Layer 1): CDSS, TSR, `gam_clr_erly_risk_score` — predict default/loss
2. **Embedded ML / third-party scores** (Layer 2): Paydex, SBFE, LexisNexis, RNN, payment-channel risk — narrower ML scores used as features
3. **Feature variables** (Layer 3): the rest — counts, ratios, ages, paydown shares grouped by concept

Identify by description: "ML model score predicting…" → L1. Third-party score name → L2. Otherwise → L3.

## Consumer vs commercial

CDSS/TSR have consumer (CPS) and commercial (SBS) versions. Only ONE is materialized per case. Column names are identical — the data doesn't self-label. Establish portfolio from `crossbu_cards.card_portfolio` and label in findings.

## Lane discipline

`model_scores` carries spend-derived FEATURES (rolling sums, normalized indices) — NOT canonical transaction totals. Never report "total spend = $X" from `model_scores`. That comes from `spends_data.Amount` (owned by `spend_payments`). Surface the model's SCORE response instead.

## Score drivers (`score_drivers`)

Per-`trans_month` snapshot: `top_<score>1..5` / `bottom_<score>1..5` → features pushing scores up/down. Pair with `model_scores` values by `trans_month` to bridge feature breaches → score moves.

## Wire-format quirks

- Some monetary columns: string with "X thousands"/"X millions" suffix → strip + multiply
- Some numerics: quoted strings ("668.00") → parse as float
- Comma-separated thousands ("9,005.00") → strip commas
- Sentinel values (`-99999999999` = "no mortgage") → filter before aggregating

## Performance

Always pass `columns=` to `query_table` — `model_scores` is 50+ cols. Always include `trans_month`. Anchor "recent/last N months" to `cut_off_date`.
