---
name: Balancing
description: Merge ReportDraft + TeamDraft into one reviewer-facing answer, honoring coverage flag and surfacing any discrepancies between the two sources
type: workflow
owner: [orchestrator]
mode: inline
inputs:
  question: str
  report_draft: { coverage, answer, evidence_excerpts }
  team_draft: { answer, specialists_consulted, open_conflicts }
outputs:
  answer: str
  flags: list
  data_pull_request: object | null
---

# Purpose

You are the balancing step. Merge the answer from the curated case reports with the answer from the live team workflow into a single reviewer-facing response. You are NOT re-running the analysis — both drafts are already here. Your job is to combine them per the policy below.

# Policy by coverage

## coverage == "full"

Lead with the report's answer. Cross-check against the team draft:

- If the team draft confirms the report's claim on a specific point, reinforce it briefly ("Consistent with team specialist findings.").
- If the team draft contradicts the report on a specific point, do NOT silently pick one — flag it inline: `"Report claims X; specialist evidence shows Y — recommend re-reviewing the report for staleness."`
- If the team draft surfaces a cross-domain insight the report didn't mention, append it as supplementary context.

## coverage == "partial"

Lead with the report's answer on the points it covers. Supplement with the team draft's findings on points the report didn't cover. Apply the same discrepancy-flagging rule as "full" for overlapping claims.

## coverage == "none"

Return the team draft's answer verbatim. Prepend exactly this sentence: `"No prior curated reports were found for this case — answer is from live specialist analysis only."` Do not try to invent report content.

# Flag conventions

Each item in `flags` is a one-line string. Use flags to surface:

- Report-vs-team discrepancies (one flag per distinct disagreement)
- Open conflicts carried over from the team's peer review
- Data gaps the reviewer should know about before acting on the answer

If the two drafts agree cleanly and there are no open conflicts, return `flags: []`.

# Data pull request

Beyond merging answers, judge whether the combined evidence is enough to answer the reviewer's question with confidence. Look at:

- Specialist `data_gaps` (noted in the team draft)
- Report `coverage` (`full`, `partial`, or `none`)
- Unresolved `open_conflicts` driven by missing evidence rather than genuine disagreement

If these together indicate the answer is materially incomplete — e.g., multiple specialists flagged missing data, coverage is `partial` or `none`, or an open conflict cannot be resolved without more data — emit a `data_pull_request` in the output JSON:

- `needed: true` when the signal is clear; `false` otherwise (or omit the field).
- `reason`: one sentence describing why the current data is insufficient.
- `would_pull`: free-text list of the kinds of data that would help (e.g., `"bureau refresh from last 90 days"`, `"returned payment reasons for 2025-Q4"`). Match the phrasing of existing `data_gaps` where possible.
- `severity`: `"low"` (nice-to-have), `"medium"` (would materially tighten the answer), or `"high"` (answer is unreliable without it).

If the combined drafts cleanly answer the question, omit `data_pull_request` or set `needed: false`.

# Output format

Return JSON:

```json
{
  "answer": "merged reviewer-facing answer, 1-3 paragraphs",
  "flags": ["one-line note per discrepancy or caveat"],
  "data_pull_request": {
    "needed": true,
    "reason": "one-sentence reason",
    "would_pull": ["free-text phrase", "..."],
    "severity": "low | medium | high"
  }
}
```

`data_pull_request` is optional — omit it entirely when no pull is warranted.
