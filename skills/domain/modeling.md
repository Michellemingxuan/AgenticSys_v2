---
name: modeling
description: Modeling domain skill — `model_scores` carries output ML risk scores (CDSS, TSR), embedded third-party scores, and feature variables grouped by concept. `score_drivers` shows which features drove each score.
type: domain
owner: [base_specialist]
mode: inline
data_hints: [model_scores, score_drivers, model_scores_transaction, score_drivers_transaction]
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

**Score → column mapping** (full definitions live in the pillar glossary
"Scores" note — injected into your prompt):
- **CDSS** (the customer's) → `credit_loss_prob` column.
- **TSR** → `tot_struct_risk_score` column
- Quote both: *"CDSS (`credit_loss_prob`): X"*
- `cust_eff_se_cdss_5_180_day_score` is a MERCHANT CDSS, not the customer's —
  only cite it for merchant-side risk.

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

**"Reacted" = crossed the risk threshold — filter, don't just sort.** For
"transactions where TSR / CDSS reacted", a score *reacted* when it crossed its
catalog threshold (TSR `tot_struct_risk_score` ≥ 20, CDSS `credit_loss_prob` ≥
10 — read from `get_table_schema`). Sorting the transaction table by the score
and taking the top N returns the ALL-TIME spike (one historical moment) and
misreads "reacted" — it hides how MANY transactions reacted and WHEN. Instead
FILTER by the threshold (`filter_op="gte"`), read the crossing count, and use
`summarize_trend(..., op="count", period="month", filter…gte…)` to see whether
it's one spike or a recurring/recent pattern. **"TSR *or* CDSS"** → check EACH
score against ITS OWN threshold SEPARATELY; if one never crosses in the window
(CDSS often peaks below 10 here), state that explicitly rather than answering
with the other score alone. **"recently / lately"** → a recent window anchored
to the DATA CUT-OFF date (not today); add it as a second AND-filter so you
surface the recent crossings, not the historical peak. See § "transactions
where TSR/CDSS reacted" in data_query.md for the exact `transaction_detail`
compound-filter call.

**"The spike" / "when it reacted" / "recently reacted" → identify the spike from
the TREND; distinguish GLOBAL from LOCAL.** A score's history usually has more
than one reaction: a GLOBAL peak (the all-time maximum) AND one or more LOCAL
spikes (a bucket that re-crosses the threshold after a lull). Get them from
`summarize_trend` — the count-by-month crossing series makes both visible (e.g.
a 2024 peak, then months of calm, then a fresh cluster of crossings near the
cut-off). Resolve the referent by the wording:
- **"recently / lately reacted"** → the MOST RECENT local spike (the latest
  bucket that crossed the threshold, near the cut-off), NOT the global peak. If
  you answer with the historical peak here, you have mis-resolved "recently".
- **"the biggest / the spike"** (unqualified) → the global peak.
- If a PRIOR turn already established the TSR/CDSS trend (it is in your KP cache /
  warmth hint), reuse those detected spikes to pin the window — don't re-derive
  "recently" as a blind last-N-months guess, and don't silently collapse it onto
  the global peak. Then pull that window's transactions for the driver step.

**Drivers are per-score AND directional — summarize BOTH top and bottom, for
EACH score.** `top_<score>*` = features pushing that score UP (raising risk);
`bottom_<score>*` = features pushing it DOWN (mitigating). **CDSS and TSR have
DIFFERENT drivers** — never merge them: report CDSS drivers (`top_/bottom_cdss*`)
and TSR drivers (`top_/bottom_tsr*`) separately. A driver summary that omits the
bottom set, or lumps CDSS and TSR together, is incomplete.

**Per-transaction drivers ARE available — never say they aren't.** For "drivers
for these transactions", "why did this transaction score high", or any
per-transaction driver question, the drivers live in `score_drivers_transaction`
keyed on `txn_date_time` (`top_/bottom_cdss*` for CDSS, `top_/bottom_tsr*` for TSR).
Do NOT reach for the MONTHLY `score_drivers` (`trans_month`) — it can't tie to a
specific transaction — and do NOT conclude "driver details are unavailable."
Fastest path: **`transaction_detail`** returns each transaction's scores AND
drivers in one call, and can select the transactions by a model metric —
`transaction_detail(base_table="model_scores_transaction", filter_column="tot_struct_risk_score", filter_op="gt", filter_value="20", sort_by="tot_struct_risk_score", sort_desc=true, limit=20)`.
Or query `score_drivers_transaction` directly, filtering `txn_date_time` by the
transactions' timestamps (`filter_op="in"` for a set).

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

**Narrate the FULL arc, through the latest bucket.** `summarize_trend` returns
the complete monthly series (it is NOT truncated for month/quarter/year grain —
`summary.last` is the cut-off month). Describe the trajectory from first bucket
to `summary.last`, quoting the last-period value — e.g. "TSR peaked at 39.6 in
2024-09, then eased to 7.7 by 2025-06". Do NOT stop the narrative at the spike
and leave the recent months unspoken; the reader sees the chart runs to the
cut-off and expects the text to cover it.

**(Optional)** If asked WHY, add driver query in R1:
```
query_table('score_drivers', columns='trans_month,top_cdss1,top_cdss2,bottom_cdss1,top_tsr1,top_tsr2,bottom_tsr1')
```
If driver columns are empty, probe `get_table_schema('score_drivers')` for actual column names.

**Hard cap: 2 tool calls.** Don't `get_table_schema('model_scores')` — you already know the column names.

────────────────────────────────────────

# § CONCEPT QUESTIONS — layered approach (≤ 2 rounds)

For concept-scoped questions ("spending features?", "delinquency signals?", "risk indicators?"):

**R1:** Probe schema + batch query in ONE round:
1. If a **§ DIRECTED VARIABLES** block was prepended to your input, treat it as directional starting points — begin with those columns and query what's relevant to the asked concept, not necessarily every one listed. Otherwise `get_table_schema('model_scores')` — read descriptions to find matching columns
2. Map concept → columns using the **§ Concept → variable selection** guidance below
3. Pick 3-6 MOST RELEVANT columns **for the asked concept**
4. `batch_summarize_trend(...)` with all selected columns

**R2:** Emit SpecialistOutput. Charts render automatically.

**Total: 2 rounds.** Do NOT schema in R1 → trend in R2 → query_table in R3.

**KP relevance filter.** When the KP cache holds data from a prior turn (e.g. TSR/CDSS trajectories), only reference it if it directly answers the current concept question. 

## Concept → variable selection

The orchestrator directs each sub-question with `concepts=[...]`; when it does, a **§ DIRECTED VARIABLES** block (variable · meaning · threshold) is prepended to your input as directional starting points — begin there and use your judgment; you needn't trend every listed variable. If no directed block is present (or you need the full set), call `get_table_schema('model_scores')` and map the concept to columns by reading descriptions. Concept vocabulary: internal_delinquency, external_delinquency, exposure_leverage, capacity_paydown, oop, spend_pattern, trends_tenure, bureau_derived, risk_events, output_score, third_party_score. See §OOP below for the interpretation that tags alone don't carry.

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

## Out-of-pattern (OOP) & exposure-vs-remit reads

"Remit" = the customer's repayment/remittance to the bank. Several OOP ("out of pattern") variables exist — read each column's description, don't assume one OOP equals another.

- **`cust_expsr_avg_rem_12m_ratio` — the pure OOP.** Customer Exposure ÷ (Average Remit over months 2–12), i.e. *how much the customer owes / spends on the bank now vs. what they've actually been paying back over the trailing year*. High ratio (**> 3.15**) = exposure has outrun recent repayment — carrying far more than the last-12-months of payments support. When the reviewer says just "OOP", this base ratio is what they mean.
- **`oop_interaction` — the adjusted OOP.** *Out of Pattern Spend index wrt Exposure* — a derived index built on the same exposure-vs-remit foundation but adjusted (spend-pattern weighted). Risky **> 28**. Use it as the model's refined read, alongside — not instead of — the base ratio.
- **`avg_remit_minus_max`** — the average remit amount (USD) itself; the denominator behind the ratio (wire-format: "thousands" suffix → strip + ×1e3). A falling avg remit alongside a rising exposure ratio confirms the squeeze is repayment-side, not just exposure-side.

**Prefer the variable over a from-scratch calculation.** `cust_expsr_avg_rem_12m_ratio` already encodes the spend/exposure-vs-repayment relationship on a fixed 12-month remit basis — so for *"is the customer paying back what they charge / spend-to-payment ratio"* questions, **cite this variable rather than recomputing total-spend ÷ total-payment from the raw tables.** Reviewers deliberately pull payments over a *longer* window (to read the payment-behaviour pattern) and spend over a *shorter* window (to see what happened right before default); a naive total/total ratio over those mismatched windows can be flat-out wrong. The model variable sidesteps that.

**General principle — read the variable, don't re-derive it.** When a `model_scores` variable already materializes a concept (a ratio, an index, an out-of-pattern measure), quote its value + catalog threshold rather than re-deriving the metric from raw transaction tables. Re-derivation risks window mismatch, wire-format (thousands/millions) errors, and disagreeing with what the model actually scored on. Re-derive only when no variable captures the concept, or to provide transaction-side *evidence* underneath the variable (label it as such).

Other OOP variants may surface as **score drivers** (e.g. `cust_debt_incom_oop` = exposure out-of-pattern vs. similar-income peers; `cust_out_pat_spend_rt` = last-month vs. first-11-months spend ratio) — probe `score_drivers` / the schema descriptions before treating them as interchangeable.

## Wire-format quirks

- Some monetary columns: string with "X thousands"/"X millions" suffix → strip + multiply
- Some numerics: quoted strings ("668.00") → parse as float
- Comma-separated thousands ("9,005.00") → strip commas
- Sentinel values (`-99999999999` = "no mortgage") → filter before aggregating

## Performance

Always pass `columns=` to `query_table` — `model_scores` is 50+ cols. Always include `trans_month`. Anchor "recent/last N months" to the CASE CUT-OFF in § DATA COVERAGE (derived from this case's data), never to today.
