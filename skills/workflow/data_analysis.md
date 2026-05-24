---
name: Data Analysis
description: Base Specialist's analysis step — given queried data, produce findings and evidence that answer the question
type: workflow
owner: [base_specialist]
mode: inline
---

Given data from tool results, produce a `SpecialistOutput`:

- `findings`: 1-2 sentence summary — the main takeaway. Numbers > prose.
- `evidence`: ≤3 specific citations (e.g., "payment_status=return: count=5").
  Quote dates and amounts verbatim. Skip if findings already contains them.
- `data_gaps`: flag only gaps that materially affect THIS answer.

Rules: every claim must trace to a tool result. Don't speculate. Don't
extrapolate from empty windows. See § DATA ANALYSIS in data_query.md for
the full anti-hallucination rules.
