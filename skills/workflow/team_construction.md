---
name: Team Construction
description: Orchestrator's team-selection routing — concept → specialist + sub-question framing
type: workflow
owner: [orchestrator]
mode: inline
replaces: [SELECT_TEAM_PROMPT, SPLIT_SUBQUESTIONS_PROMPT]
---

Pick specialist tool(s) to call and frame each one's sub-question. The team roster is wired as tools; team selection = which tools to call. Output is the tool calls you emit next, not JSON.

## Concept → specialist (single-domain routing)

| Reviewer phrasing | Specialist |
|---|---|
| FICO, bureau score, tradelines, external delinquency, derog marks | `bureau` |
| DTI, income, affordability, capacity, limit headroom | `capacity_afford` |
| **cards (count/balance/limit), consumer/commercial card, cross-product exposure, portfolio mix** | `crossbu` |
| **top merchants the customer spends with, merchant concentration, recurring merchants, per-merchant trend** | `spend_payments` (transaction-level on `spends_data`, NOT `crossbu`) |
| tenure, customer relationship, product usage history | `customer_rel` |
| internal model output scores (CDSS / TSR / credit-loss / GAM / PD), model trajectory, score drivers | `modeling` |
| embedded ML / third-party scores (Paydex, SBFE, LexisNexis, RNN, payment-channel risk) | `modeling` (Layer-2 columns on `model_scores` — see modeling skill) |
| payment volume, payment returns, success-vs-return ratio, settled-vs-cleared payments | `spend_payments` |
| **DPD / days past due / internal delinquency index / payment-behavior trajectory / minimum-due-only history** | `modeling` (Layer-3 indicator features). The raw `payments` table CANNOT answer DPD. |
| WCC, agent call notes, customer-service log, collections call | `wcc` |

## Cross-domain topics (multi-specialist — DON'T single-route)

| Topic | Team | Each specialist's slice |
|---|---|---|
| **spending / spend pattern / merchant concentration** | `spend_payments` + `modeling` (+ `crossbu` only if explicitly B2B) | `spend_payments` = transaction-level + merchant concentration. `modeling` = ML-derived spend features (out-of-pattern, concentration risk-rate, time-weighted spend). A spending answer with only `spend_payments` is incomplete — it misses the model's view of the spend shape. |
| **default journey / DPD progression** | `bureau` + `modeling` | `bureau` = external default tradelines + derogs. `modeling` = score evolution + driver rotation + internal delinquency indicators. |
| **delinquency / payment-deterioration trajectory** | `modeling` + `spend_payments` (+ `bureau` only if "external" is explicit) | `modeling` = stage-of-delinquency indicators (DPD counts, internal indices, return indices, min-due-only). `spend_payments` = settlement-attempt side (success/return counts + reasons). Indicators give the *stage*; payments give the *attempts*. |
| **exposure / total customer risk** | `crossbu` + `bureau` + `capacity_afford` (+ `modeling` for rolled-up ratio / leverage view) | `crossbu` = card balances/limits. `bureau` = external exposure. `capacity_afford` = vs income/headroom. `modeling` = model-rolled-up exposure & leverage ratios. |
| **broad / "full review"** | all specialists | Only when genuinely cross-domain. |

For everything else, single- or 2-specialist teams. Widen to 3+ only when the table above says so.

**Edge cases:**
- balance vs spend: balance is `crossbu_cards.balance` (point-in-time); spend is a flow. Don't substitute.
- "how many cards" → `crossbu` (NOT `customer_rel` — that owns only tenure).
- merchant concentration of customer's spending → `spend_payments`. The `crossbu_merchants` table is B2B charge volume those businesses *receive* — different concept; routing customer-side merchant questions to `crossbu` is a known mis-route.

If phrasing doesn't match the table, fall through to the auto-generated TEAM ROSTER (`owns: <table>` lines) and route by which table carries the answer.

## Subject vs object — route to the SUBJECT

When a specialist appears as the grammatical subject, route there regardless of the predicate.

- "Does **X** have info about Y?" / "What does **X** say about Y?" → X
- "What is the customer's Y?" / "How many Y?" (no subject) → route to Y owner

Examples: "Does **the model** have info about spending?" → `modeling`. "Does **WCC** show complaints about cards?" → `wcc`. "What does **the bureau** say about payment history?" → `bureau` (NOT `spend_payments`). ("the model" / "the models" ALWAYS = internal ML risk-scoring models, never the agent system.)

## Selection rules

1. **Minimum set** — but the Cross-domain table above is the authoritative team size for matching topics; don't shrink those.
2. Every pick carries weight — no "for context" / "in case relevant".
3. Match data, not name (`customer_rel` ≠ "questions about the customer").
4. **Follow-ups REUSE THE PRIOR TEAM.** When the new question is in the same domain as the previous turn (or a near-paraphrase), reuse the exact same team. Each specialist carries a session-scoped knowledge base (`CaseSession.specialist_kb`) prepended as a digest to every new sub-question — so reusing them lets each specialist build on what they already found, instead of restarting. Don't reshuffle for follow-ups that are the same question with small variations.
5. **Always pair with `report_agent`** on the same turn (see TOOL-USE DISCIPLINE). They run in parallel.
6. **Read `[KB-warmth: …]` hint when present.** The user message may begin with `[KB-warmth: spend_payments (5 KPs), modeling (3 KPs). …]`. This is the primary follow-up routing signal — prefer warm specialists for in-domain follow-ups. The hint is informational and is NEVER part of the question itself; route as if it weren't in the user's text.

## Sub-question framing

- Serves the root — if the specialist's answer wouldn't change the final answer, drop it.
- Stays in the specialist's domain.
- Uses the specialist's data vocabulary (name the column/table when you know it).
- Orthogonal across specialists — no duplicates.
- One specialist → sub-question may equal the root question verbatim.
