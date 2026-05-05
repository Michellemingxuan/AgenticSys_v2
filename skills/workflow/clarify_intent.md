---
name: Clarify Intent
description: When the in-scope question is ambiguous, surface 2–4 candidate interpretations for the reviewer to pick from BEFORE the orchestrator is triggered
type: workflow
owner: [chat_agent]
mode: inline
inputs:
  question: str
outputs:
  needs_clarification: bool
  options: list[str]
  reason: str
---

You decide whether the question's intent is clear enough to dispatch directly to the orchestrator, or whether the reviewer should pick between candidate interpretations first. Run AFTER scope check (only on in-scope questions).

## Pass-through (`needs_clarification: false`)

The question's subject, metric, and (if relevant) time-window are all unambiguous. Examples:

- "How many consumer cards does this customer have?" — subject = card count; classifier known (consumer = `card_portfolio == 'CPS'`).
- "What's the customer's FICO score?" — single bureau metric.
- "Show me all returned payments" — single concrete filter.
- "Why did TSR not react at the start of ramp-up?" — subject = TSR; ramp-up is data-derived per the pillar glossary.

When pass-through, return `options: []` and `reason: ""`.

## Clarify (`needs_clarification: true`)

Surface 2–4 candidate questions when ANY of these hold:

- **Multiple specialists could plausibly own the answer** with different framings. e.g. "Is the customer at risk?" — bureau-only? model-score-only? cross-domain summary?
- **Subject is underspecified.** "Show me the cards" — consumer cards? commercial cards? all cards? delinquent cards only?
- **Metric / aggregation is ambiguous.** "How much has the customer paid?" — total paid amount? count of payments? average per month?
- **Time-window word without anchor.** "Recently" — last 3 months? last 6 months? since the most recent delinquency?
- **Pronoun / deictic without a clear antecedent.** "What about the others?" / "Show me those" — when prior turn doesn't pin the referent.

When clarifying, each option MUST:

- Be a fully-formed question that the orchestrator can dispatch directly (no further clarification needed).
- Reference the specific concept / column / window that disambiguates it.
- Be distinct from the other options — different intents, not paraphrases of one.

Aim for 2–3 options. 4 only when 4 genuinely-different intents exist.

## Format

When `needs_clarification: false`:

```json
{ "needs_clarification": false, "options": [], "reason": "" }
```

When `needs_clarification: true`:

```json
{
  "needs_clarification": true,
  "options": [
    "<reformulated unambiguous question 1>",
    "<reformulated unambiguous question 2>",
    "<reformulated unambiguous question 3>"
  ],
  "reason": "<one sentence on why the original was ambiguous>"
}
```

Keep `options` to plain question strings — no numbering, no bulletpoints, no metadata. The harness adds those when displaying to the reviewer.
