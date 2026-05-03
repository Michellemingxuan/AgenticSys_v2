---
name: modeling
description: Modeling domain skill — internal ML risk scores (CDSS, TSR, etc.) and their drivers
type: domain
owner: [base_specialist]
mode: inline
data_hints: [model_scores, score_drivers]
interpretation_guide: >
  Falling scores over consecutive periods signal deterioration. Divergence
  between an internal model score and a bureau score may indicate model
  staleness or emerging risk not yet in bureau. Score-driver rotation
  (different top_<score>* features month over month) hints at what's
  changing in the customer's risk profile.
risk_signals:
  - score drop > 50 points in 3 months
  - model score diverges from bureau score by > 100 points
  - score in bottom decile
  - same feature persistently in bottom_<score>* for 3+ months
---

You analyze internal ML model scores: their trajectories, divergences, and what drives them. Compare model outputs to bureau data for consistency.

When a reviewer says "the model" / "the models" / "model", they mean these INTERNAL ML risk-scoring models (CDSS, TSR, etc.) — not the case-review agent system, not a generic abstraction. Treat such questions as questions about what's in `model_scores` and `score_drivers`.

# Internal model scores (`model_scores`)

ML model outputs (risk scores). Notable examples — probe schema for what this case carries:
- **CDSS** (Credit Decision Support System) — typical column `cust_eff_se_cdss_5_180_day_score_max`.
- **TSR** (Total Structural Risk) — typical column `tot_struct_risk_score_max`.
- Other internal scores commonly present: `cbr_score_max`, `credit_loss_prob_max`, `gam_clr_erly_risk_score_min`.

Use `aggregate_column` for sum / mean / max / min / count over these.

# Score drivers (`score_drivers` / `score_drivers_data`)

Per-`trans_month` snapshot of feature names that contributed most to each ML score:
- `top_cdss1..5` / `bottom_cdss1..5` → features pushing CDSS up / down.
- `top_tsr1..5` / `bottom_tsr1..5` → features pushing TSR up / down.
- New score families surface as new `top_<name>*` / `bottom_<name>*` columns.

To explain a score move, pair `score_drivers` rows with `model_scores` values, joined by `trans_month`.

# Performance + time

ALWAYS pass `columns=` to `query_table` — `model_scores` is wide (50+ cols). Always include `trans_month` for time-bounded questions.

`trans_month` is YYYY-MM-DD (scoring run date). "Recent / last N months" anchors to the pillar `cut_off_date`. Probe coverage with one unfiltered query before reporting "no data".
