---
name: Data Analysis
description: Base Specialist's analysis step — given queried data, produce findings, evidence, and implications that answer the question
type: workflow
owner: [base_specialist]
mode: inline
---

Given a question and the rows returned by `query_table`, synthesise findings and produce an answer.

**Output shape:**

- `findings`: 1-2 sentence summary — the main takeaway in answer to the question.
- `evidence`: list of specific row-level citations (e.g., "payment_date=2024-09-24, return_flag='returned'"). Quote dates and amounts verbatim from the returned rows; never from filter bounds.
- `data_gaps`: list of things that would sharpen the answer but weren't available (empty if none).

**Rules:**

- Every claim in `findings` must be supported by a row-level citation in `evidence`.
- Do not speculate beyond the returned rows. If a window is empty, say so explicitly and mark the gap — don't extrapolate.
- If the data contradicts prior rolling-summary entries, flag the contradiction rather than silently updating the narrative.
- Respect the time & date discipline from the data_query skill when citing or comparing rows.
