---
name: Report Needle
description: Pick relevant curated report files for a reviewer question; judge coverage
type: workflow
owner: [report_agent]
mode: inline
inputs:
  question: str
  available_files: list[str]
outputs:
  relevant_files: list[str]
  coverage: "explicit|implicit|not_mentioned"
  hints: list[str]
---

Pick the report file(s) whose content bears on the answer, then judge coverage honestly. The taxonomy is INTENTIONALLY narrow at the top end — only `explicit` when the report literally states the answer.

Coverage:
- `explicit` — the report directly states the answer (or the specific facts the question asks for, in a form a reader could quote without inference). Don't use this just because the report touches on the topic.
- `implicit` — the report contains relevant facts but the answer requires INFERENCE / SYNTHESIS from those facts. Topic is covered; specific answer is not. This is the default when the report only frames or partially addresses the question.
- `not_mentioned` — the report doesn't cover the question's topic (folder empty, or no listed file plausibly addresses it).

Bias: when in doubt, prefer `implicit` over `explicit`. Most curated reports give context, not direct answers — claiming `explicit` when the answer is being inferred over-states the report and crowds out specialist data in downstream balancing. Use `explicit` ONLY when you can quote a verbatim line from the report that IS the answer.

## Concept → file (canonical `<domain>_exp_0.md` layout)

| Reviewer concept | File |
|---|---|
| FICO / bureau score / external tradelines / external delinquency | `bureau_exp_0.md` |
| default journey / DPD progression / DPD timeline | `default_journey_exp_0.md` |
| **cards (count/balance/limit) / consumer/commercial card / portfolio mix / merchant relationships** | **`crossbu_exp_0.md`** |
| score drivers / risk factors | `driver_exp_0.md` |
| summary / overview / headline / broad multi-domain | `executive_summary_exp_0.md` |
| notable findings / what's interesting | `interestingness_exp_0.md` |
| internal model score / PD / GAM | `modeling_exp_0.md` |
| payments / payment returns / spend / spend spikes | `payment_spend_exp_0.md` |
| **spending pattern / spend behavior / spend trajectory / pattern questions / what's atypical / merchant exposure / recurring transactions / high-value outliers / late-stage spends** | **`payment_spend_exp_0.md` AND `interestingness_exp_0.md`** (BOTH — payment_spend has the dataset overview, interestingness has the structurally-atypical points across temporal / merchant / persistence dimensions) |
| recommended action / next step / treatment | `strategy_0.md` |
| WCC / write-off / collections | `wcc_notes_exp_0.md` |

Rules:
- Unambiguous routing match → pick the file. Coverage is `explicit` ONLY if the file directly states the answer; otherwise `implicit`.
- When in doubt, over-include — reading one extra file is cheap. Add `executive_summary_exp_0.md` as fallback when uncertain.
- NEVER return `not_mentioned` because literal keywords don't appear in filenames; translate via the table first. `not_mentioned` is only when the folder is empty OR routing+filename+domain reasoning all yield nothing.
- Filenames may not match the canonical layout — fall back to topic-hint matching against filenames.
- **Pattern / trajectory / "what's atypical" framings** are inherently multi-aspect — pull `interestingness_*` alongside the topic-domain file (e.g. `payment_spend_exp_0.md` + `interestingness_exp_0.md` for spending pattern). The interestingness report carries the cross-cutting structural observations that no single domain file contains.

Output:
```json
{ "relevant_files": ["..."], "coverage": "explicit|implicit|not_mentioned", "hints": ["one per file"] }
```
`hints` length == `relevant_files` length. If `coverage == "not_mentioned"`, both empty.
