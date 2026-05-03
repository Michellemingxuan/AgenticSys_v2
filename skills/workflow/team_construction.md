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
| **cards (count/balance/limit), consumer/commercial card, cross-product exposure, portfolio mix, merchant relationships** | **`crossbu`** |
| tenure, customer relationship, product usage history | `customer_rel` |
| internal model score, PD, GAM, model trajectory | `modeling` |
| payments, payment returns, spend, spend volume, delinquency timing | `spend_payments` |
| WCC, agent call notes, customer-service log, collections call | `wcc` |

Edge cases:
- **balance vs spend:** balance → `crossbu` (`crossbu_cards.balance`); spend → `spend_payments` (`spends_data.Amount`). Different concepts, different specialists.
- **default journey / DPD progression:** primary `bureau`; cross-check `modeling` if score evolution matters.
- **broad / "full review":** select all specialists. Otherwise narrow to 1–2.
- **"how many cards":** `crossbu` (NOT `customer_rel`, despite the name — it owns only the tenure table).

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
4. **Follow-ups:** prefer the specialist consulted on the prior turn if the same domain — its history is preserved within the AppContext.
5. **Always pair with `report_agent`** on the same turn (TOOL-USE DISCIPLINE rule below). They run in parallel.

## Sub-question framing

- Serves the root — if the specialist's answer wouldn't change the final answer, drop it.
- Stays in the specialist's domain.
- Uses the specialist's data vocabulary (name the column/table when you know it).
- Orthogonal across specialists — no duplicates.
- One specialist selected → sub-question may equal the root question verbatim.
