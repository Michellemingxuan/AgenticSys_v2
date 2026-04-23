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

# Output format

Return JSON:

```json
{
  "answer": "merged reviewer-facing answer, 1-3 paragraphs",
  "flags": ["one-line note per discrepancy or caveat"]
}
```
