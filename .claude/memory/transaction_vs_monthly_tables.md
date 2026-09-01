---
name: transaction-vs-monthly-tables
description: Transaction-level tables (model_scores_transaction / score_drivers_transaction) answer per-txn and approve-deny questions; monthly tables answer trends. Filter day-grain `trans_dt` by default, `txn_date_time` only for within-day precision.
metadata:
  type: project
---

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
