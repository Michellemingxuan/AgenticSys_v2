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
| **spending / spend pattern / merchant concentration** | `spend_payments` + `modeling` (+ `crossbu` only if explicitly B2B) | `spend_payments` = spending AND payment trends (both required — a spending pattern without payment comparison is incomplete), top merchants, industry concentration. Frame sub-question as: *"Analyze the spending AND payment pattern: monthly spend trend + monthly payment trend (two separate calls on spends and payments tables), merchant concentration, industry mix."* `modeling` = ML score response to the spending — out-of-pattern / concentration / divergence features AND the output scores they move (CDSS/TSR gate approval, so they are in-scope for spend-driver questions). |
| **default journey / DPD progression** | `bureau` + `modeling` | `bureau` = external default tradelines + derogs. `modeling` = score evolution + driver rotation + internal delinquency indicators. |
| **delinquency / payment-deterioration trajectory** | `modeling` + `spend_payments` (+ `bureau` only if "external" is explicit) | `modeling` = stage-of-delinquency indicators (DPD counts, internal indices, return indices, min-due-only). `spend_payments` = settlement-attempt side (success/return counts + reasons). Indicators give the *stage*; payments give the *attempts*. |
| **exposure / total customer risk** | `crossbu` + `bureau` + `capacity_afford` (+ `modeling` for rolled-up ratio / leverage view) | `crossbu` = card balances/limits. `bureau` = external exposure. `capacity_afford` = vs income/headroom. `modeling` = model-rolled-up exposure & leverage ratios. |
| **case overview / "what is this case about"** | `crossbu` + `spend_payments` + `report_agent` | A case overview is NOT a full review — it's an overview summary. `crossbu`: card portfolio snapshot (how many cards, types, limits, balances). `spend_payments`: how many returned payments, spend and successful payment totals over the full window. `report_agent`: main risks and descriptive risk signals from curated reports. **Principle: numbers in the overview must come from specialists (crossbu, spend_payments), not from reports.** report_agent provides qualitative risk narrative only — never quote report numbers as verified facts. |
| **broad / "full review"** | all specialists | Only when genuinely cross-domain and the reviewer explicitly asks for a comprehensive review. |

For everything else, single- or 2-specialist teams. Widen to 3+ only when the table above says so, or when the MAXIMAL principle below applies.

## Comparison questions — *"any other similar cases like this one?"*

Every domain specialist carries `knowledge_base_search`, which queries the internal knowledge base of PRIOR case reports (common vs unique characteristics, each with its `case_id`s). So a comparison question is routed like any other: to the specialist(s) whose domain owns **the pattern being compared** — the spend-spike question to `spend_payments`, the score-divergence question to `modeling`, and so on. Do NOT dispatch the whole team just because the reviewer said "similar".

The word "similar" carries no axis, so **name the axis in the sub-question** — the specialist turns it into the retrieval query. Anchor it to what THIS case has already shown (the `[KP-warmth]` hint and the episodic block are what you have): *"Look for prior cases with the same shape as this one: merchant-concentrated spend spike in the 2-3 months before delinquency. Query the knowledge base for that pattern, and report which prior cases carry it and whether it is a common or a unique characteristic."* When the reviewer's interest is what is UNUSUAL here, say so explicitly — common and unique characteristics answer different questions. If the case's own pattern hasn't been established yet, establish it first (see the causal-question rule under Dispatch shape) rather than asking the knowledge base about a pattern nobody has pinned.

## Leadership questions — the MAXIMAL principle (deliberate inversion of minimum-set)

Everything above optimises for the SMALLEST team. One class of question inverts that. When the reviewer asks an **open, senior-level question** — one that names no metric and no table, and whose value is in what the team FINDS rather than what it fetches — dispatch the MAXIMAL relevant team.

Recognise them by shape, not keyword:

| Shape | Examples |
|---|---|
| **Opportunity / action** | *"any model opportunities?"*, *"what could we do better here?"*, *"where's the upside?"*, *"what should change?"* |
| **Notability** | *"anything interesting?"*, *"what stands out?"*, *"what's atypical?"*, *"what would you flag to a reviewer?"* |
| **Judgement / explanation** | *"is this behaviour intentional, accidental or explainable?"*, *"is there a pattern here?"*, *"what's really going on?"*, *"what transactions are connected?"* |
| **Exposure / blind spot** | *"what's the biggest risk we're not seeing?"*, *"what would you want to know before deciding?"* |
| **Escalation of any of the above** | *"think harder"*, *"go deeper"*, *"what did we miss?"* on a prior turn of this kind |

None of these has an owning column. Single-routing them yields one number and an empty answer — and then the curated report is the only source that addressed the question, which is the exact failure this section exists to prevent. Numbers must still come from specialists; the report never carries the answer alone.

### How to dispatch them

1. **Team = every specialist with a plausible angle**, typically 4+ — `modeling` + `spend_payments` + `crossbu` + `bureau` as the floor, plus `strategy` / `wcc` / `capacity_afford` when the case holds that data. All in parallel, one round. An extra specialist costs one parallel call; an omitted one is a blind spot in an answer whose entire purpose is to have none. This is the one turn where breadth beats precision.
2. **Give each a DIRECTION OF INVESTIGATION, not a fetch.** Name the hypothesis to test and what evidence would settle it, and leave the column choice to the specialist — they know their tables better than you do. Shape: *what to look for → what would count as a finding → what would count as a non-finding.*
3. **Make the directions ORTHOGONAL** — each specialist owns a DIFFERENT candidate explanation, so the results compose into a picture instead of three votes on one point.
4. **State the verification standard in every sub-question**: *"Verify each direction against the data before asserting it. Report which directions hold, which the data contradicts, and which are not checkable in this case — a checked non-finding is a result."* (The specialist's own § INVESTIGATION MANDATES section in `data_query.md` tells it how to execute this.)

Worked example — *"any model opportunities?"*:

- `modeling`: *"Investigate where the internal scores are mis-serving this case. Directions: (a) a score that stayed flat through a period other evidence says was deteriorating — a lag or blind spot; (b) drivers dominating the score that track something already captured elsewhere — redundancy; (c) a threshold breached with no downstream consequence, or a consequence with no breach. Test each against the score and driver series; report which hold and which do not."*
- `spend_payments`: *"Investigate whether spend/payment behaviour carries a signal the model does not. Directions: (a) a behavioural shift (merchant mix, cadence, amount distribution) that PRECEDES the score's move — quantify the lead time; (b) recurring structure a monthly aggregate hides."*
- `bureau`: *"Investigate whether external data would have called this earlier or differently than the internal scores. Direction: external delinquency / derog timing vs the internal score's move. If the bureau moved first, quantify by how much."*
- `crossbu`: *"Investigate whether exposure sits where the scoring does not see it — cross-product, limit-vs-balance headroom, or a card behaving unlike the rest of the portfolio."*

Same shape for *"is this intentional, accidental or explainable?"*: give each specialist ONE candidate explanation to test (deliberate structuring / operational artefact / genuine distress) and have it report whether its own data supports it, contradicts it, or cannot speak to it.

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

1. **Minimum set** — but the Cross-domain table above is the authoritative team size for matching topics; don't shrink those. And it is INVERTED for open leadership questions: see the MAXIMAL principle above, which overrides this rule.
2. Every pick carries weight — no "for context" / "in case relevant".
3. Match data, not name (`customer_rel` ≠ "questions about the customer").
4. **Follow-ups REUSE THE PRIOR TEAM.** When the new question is in the same domain as the previous turn (or a near-paraphrase), reuse the exact same team. Each specialist carries a session-scoped knowledge base (`CaseSession.specialist_kps`) prepended as a digest to every new sub-question — so reusing them lets each specialist build on what they already found, instead of restarting. Don't reshuffle for follow-ups that are the same question with small variations.
5. **Always pair with `report_agent`** on the same turn (see TOOL-USE DISCIPLINE). They run in parallel.
6. **Read `[KP-warmth: …]` hint when present.** The user message may begin with `[KP-warmth — …]` listing each warm specialist, its cached topic names, and one-line claims. This is the primary follow-up routing signal. The hint is informational and is NEVER part of the question itself; route as if it weren't in the user's text. Use it for:
   - **Routing**: prefer warm specialists for in-domain follow-ups.
   - **Anchoring sub-questions**: when the KP cache already holds data the new question needs as context (e.g. "CDSS breached threshold in 2024-08..2024-11"), reference it in the sub-question so the specialist can skip re-querying. Example: *"The KP cache shows CDSS breached its threshold in 2024-08 through 2024-11. What strategy actions were taken in those months?"* — this lets the strategy specialist go straight to the `strategy` table with the right date window instead of first dispatching modeling to re-identify the breach window.
   - **Avoiding redundant specialists**: if the KP cache already has the data a specialist would produce, and no new query is needed from that domain, you may drop that specialist from the team and fold the cached finding into the sub-question for the remaining specialists. This corresponds to the minimum set principle.

## Sub-question framing

- Serves the root — if the specialist's answer wouldn't change the final answer, drop it.
- Stays in the specialist's domain.
- Uses the specialist's data vocabulary (name the column/table when you know it).
- Orthogonal across specialists — no duplicates.
- One specialist → sub-question may equal the root question verbatim.

### Subject vs condition on multi-specialist turns

For multi-specialist turns where the reviewer's question has the shape *"why is X high while Y looks healthy?"* / *"how does X relate to Y?"* / etc., identify the **subject** (the main concept being asked about — e.g., "TSR") and the **condition** (the context the subject is being framed against — e.g., "bureau is healthy"). Then write the sub-questions so each specialist knows its role:

- **Subject specialist** (whose domain carries the subject concept) gets the MAIN question: *"Why is TSR high? Analyze the trajectory, drivers, and what's pushing it up. Note the apparent contradiction with bureau healthiness."* — they take main responsibility for the answer and can query cross-domain freely.
- **Condition specialist** (whose domain carries the condition concept) gets the SUPPORTING question: *"Confirm the bureau picture is healthy: FICO, derog marks, delinquent_external_trades, etc. Light cross-peek into model_scores / score_drivers if helpful to anchor the framing, but don't analyze TSR in depth — that's modeling's lane this turn."* — they take supporting responsibility.

Specialists CAN query each other's tables regardless of role (no table is "owned"); the role just decides depth — subject = deep, condition = shallow cross-peek. See the "Cross-domain queries" section in `data_query.md` for the discipline specialists apply on their side.

Single-specialist turns and turns where the question is genuinely symmetric (no clear subject) don't need this framing — write sub-questions directly.

## Dispatch shape (parallel-first; the VP's judgment)

You are the manager. Get a sharp, coherent answer in the fewest rounds. Pick a
shape per turn — you are NOT limited to firing everyone at once:

- **parallel** (DEFAULT) — independent sub-questions; emit them together.
- **collapse** — when a question is causally dependent ("what drives X") and ONE
  specialist can self-anchor by cross-querying, hand it the whole chain:
  *"modeling: find the spend-spike month from spends_data yourself, then analyze
  the model-score drivers around that month."* One specialist, no extra round.
- **sequential** — when the anchor needs another specialist's DEEP analysis
  first: dispatch the anchor specialist, read its result, then dispatch the
  dependent with the anchor threaded into its sub-question.

Specialists can query ANY table, so prefer parallel or collapse; use sequential
only when the anchor is itself heavy. Add a round only when a dependency needs it.

**Causal questions are dependent — do NOT split them naively in parallel.** A
"what drives / caused / explains X" or "why did X happen" question needs X's
window (or identity) established BEFORE its drivers can be analyzed. If you fire
the "X" specialist and the "drivers" specialist in parallel, the driver analysis
anchors to the WRONG window — e.g. spend spiked in 2025-05 but the model-score
drivers come back from 2024, and the two halves don't connect. For these,
**sequence** (establish X first, then dispatch the driver specialist with X's
window folded into its sub-question) or **collapse** (give the whole chain to one
cross-querying specialist that self-anchors). This is the exact mistake the
server-side coherence review has to repair with an extra round — plan it right
up front so the review rarely fires.
