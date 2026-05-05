---
name: Team Construction
description: Orchestrator's team-selection routing — concept → specialist + sub-question framing
type: workflow
owner: [orchestrator]
mode: inline
replaces: [SELECT_TEAM_PROMPT, SPLIT_SUBQUESTIONS_PROMPT]
---

Pick specialist tool(s) to call and frame each one's sub-question. The team roster is wired as tools; team selection = which tools to call. No JSON output for this step — the output is the tool calls you emit next.

## Concept → specialist

| Reviewer phrasing | Specialist |
|---|---|
| FICO, bureau score, tradelines, external delinquency, derog marks | `bureau` |
| DTI, income, affordability, capacity, limit headroom | `capacity_afford` |
| **cards (count/balance/limit), consumer/commercial card, cross-product exposure, portfolio mix** | **`crossbu`** |
| **top merchants the customer spends with, merchant concentration, recurring merchants, per-merchant trend** | **`spend_payments`** (transaction-level on `spends_data`, NOT crossbu) |
| tenure, customer relationship, product usage history | `customer_rel` |
| internal model score, PD, GAM, model trajectory | `modeling` |
| payments, payment returns, payment volume, delinquency timing | `spend_payments` |
| WCC, agent call notes, customer-service log, collections call | `wcc` |

## Cross-domain topics (multi-specialist)

Some concepts span more than one specialist's data. For these, build a **team of 2–3** specialists, each answering a complementary slice. Don't single-route.

| Topic | Specialists to consider | Their slice |
|---|---|---|
| **spending / spend pattern / spend volume / merchant concentration** | `spend_payments` + `modeling` (+ `crossbu` only when the question is explicitly B2B) | `spend_payments`: transaction-level spend AND merchant-name / merchant-industry concentration of the customer's own spending — `spends_data.Amount`, `Merchant Name`, `Merchant Industry`. **All "top merchants / recurring merchants / per-merchant trends" routes here, not crossbu.** `modeling`: ML-derived spend features (concentration, divergence, out-of-pattern indices) feeding the risk scores. `crossbu` belongs ONLY when the reviewer asks about the *merchant side* of the customer's businesses (B2B charge volume those businesses receive, via `crossbu_merchants.merchant_charge_volume`) — a different concept than the customer's own purchasing behavior. |
| **default journey / DPD progression** | `bureau` + `modeling` | `bureau`: external default tradelines, derog marks. `modeling`: score evolution leading into default + driver rotation. |
| **exposure / total customer risk** | `crossbu` + `bureau` + `capacity_afford` | `crossbu`: card balances and limits. `bureau`: external exposure. `capacity_afford`: vs income / capacity headroom. |
| **broad / "full review"** | all specialists | Only when the question is genuinely cross-domain or asks for a complete picture. |

For everything else, single-specialist or 2-specialist teams are normal. Only widen to 3+ when the topic is genuinely cross-domain (per this table).

Other edge cases:
- **balance vs spend:** balance is `crossbu_cards.balance` (point-in-time outstanding); spend is a flow quantity. Different concepts — don't substitute one for another even when both can come back from `crossbu`.
- **"how many cards":** `crossbu` (NOT `customer_rel`, despite the name — it owns only the tenure table).
- **merchant concentration of customer's spending:** `spend_payments` (via `spends_data.Merchant Name` / `Merchant Industry`). NOT `crossbu`. The `crossbu_merchants` table is the *merchant-side* receipts angle for the customer's businesses (B2B), a different concept entirely; routing customer-side merchant-concentration questions to crossbu is a known mis-route.

If phrasing doesn't match the table, fall through to the auto-generated TEAM ROSTER (`owns: <table>` lines) and route by which table carries the answer.

## Subject vs object — route to the SUBJECT

When a specialist appears as the grammatical subject of the question, route there regardless of what concept appears in the predicate.

| Shape | Subject = | Object = |
|---|---|---|
| "Does **X** have information about Y?" | X | Y |
| "What does **X** say about Y?" / "Does **X** cover / track Y?" | X | Y |
| "Show me **X**'s view of Y" | X | Y |
| "What is the customer's Y?" / "How many Y?" | (no subject) | route to Y owner |

Examples:
- "Does **the model** have info about external delinquency?" → `modeling`. ("the model" / "the models" in reviewer questions ALWAYS = internal ML risk-scoring models — never the agent system or a generic abstraction.)
- "Does **WCC** show complaints about cards?" → `wcc` (cards is the topic, WCC is the data source).
- "What does **the bureau** say about payment history?" → `bureau` (NOT `spend_payments`).

## Selection rules

1. Minimum set. 1 is normal; 2 when the question explicitly spans domains; 3+ only on a "full review".
2. Every pick must carry weight — no "for context", no "in case relevant".
3. Match data, not name (`customer_rel` ≠ "questions about the customer").
4. **Follow-ups & near-duplicates — REUSE THE PRIOR TEAM.** Read the conversation context. When the new question is in the same domain as the previous turn (e.g. another spending question after a spending question, another bureau question after a bureau question), or is a near-paraphrase of an earlier question, reuse the EXACT SAME team — same specialists, same roles. They retain per-specialist conversation memory across turns within this session (the wrapper preserves each one's input/output history per `AppContext._specialist_histories`), so reusing them lets each specialist build on what they already discovered instead of restarting from cold. **Do not** reshuffle the team for a follow-up that's effectively the same question with a small variation; only widen / narrow the team when the topic genuinely shifts. Same team + new sub-question = the cheapest, most coherent follow-up.
5. **Always pair with `report_agent`** on the same turn (TOOL-USE DISCIPLINE rule below). They run in parallel.

## Sub-question framing

- Serves the root — if the specialist's answer wouldn't change the final answer, drop it.
- Stays in the specialist's domain.
- Uses the specialist's data vocabulary (name the column/table when you know it).
- Orthogonal across specialists — no duplicates.
- One specialist selected → sub-question may equal the root question verbatim.
