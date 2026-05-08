---
name: Report Analysis
description: Extract an evidence-grounded answer from selected curated report files
type: workflow
owner: [report_agent]
mode: inline
inputs:
  question: str
  report_content: str
outputs:
  answer: str
  evidence_excerpts: list[str]
---

Extract a tight evidence-grounded answer from the report text given.

Rules:
- Quote short excerpts verbatim in `evidence_excerpts` (one line each, in double quotes).
- Don't fabricate content. If the reports don't answer a part of the question, acknowledge the gap.
- If reports disagree on a point, surface the disagreement and name both sources.
- Tight answer: 1 paragraph, 3–6 sentences, 2–5 evidence excerpts.
- Preserve numbers, dates, names verbatim from source. Numeric values arrive comma-formatted (e.g. `$174,807.36`); quote them in that exact form. NEVER strip commas or re-encode (`174807.36` would be masked to `***MASKED***.36` by the redaction layer).
- **Match concept, not number shape.** Balance ≠ spend ≠ payment amount. Card limit ≠ balance ≠ utilisation. Returned-count ≠ successful-count ≠ total-count. Number of cards ≠ card-month rows. If the report has a `"$1,200,700 total spend"` figure and the reviewer asked about balance, DO NOT quote it — wrong concept. Find the matching figure or acknowledge the gap.

Output:
```json
{ "answer": "...", "evidence_excerpts": ["\"...\""] }
```
