---
name: Balancing
description: Merge ReportDraft + TeamDraft. Specialist live-data evidence outranks curated report text on factual claims.
type: workflow
owner: [orchestrator]
mode: inline
inputs:
  question: str
  report_draft: { coverage, answer, evidence_excerpts, files_consulted }
  team_draft: { answer, specialists_consulted, evidence, raw_data, open_conflicts, data_gaps }
outputs:
  answer: str
  flags: list
  data_pull_request: object | null
---

You merge ReportDraft (curated) + TeamDraft (live specialists) into the reviewer-facing answer. Don't re-run analysis — combine what's given under the rules below.

## Evidence hierarchy (apply BEFORE coverage policy)

Strongest → weakest on any data-shaped claim (counts, amounts, dates, scores, presence/absence):

1. Specialist findings backed by a live `query_table` / `aggregate_column` result (cites `table.column`, has non-empty `raw_data`, or quotes a comma-formatted value from a tool response).
2. Specialist findings reasoned from observed fields (no fresh query, but real columns cited).
3. Curated report text (`evidence_excerpts`, `files_consulted`) — strong on narrative/historical context, weakest on live numbers.
4. General-knowledge / schema-inference — should not appear; demote and flag.

**Default trust direction on factual claims: specialist data > report text.** The report's strength is framing, not numbers.

## Coverage policy

Coverage describes how the report RELATES to the question — not how reliable it is. Apply the hierarchy regardless of flag value.

- `explicit` — the report directly states the answer. Cross-check against specialist data:
  - Confirm → cite both (report file + table.column).
  - Contradict on a factual point → lead with specialist data, flag report as potentially stale.
  - Specialist surfaces new data → fold in as primary, not footnote.
  - Specialist `data_gap` on the same point → defer to the report's explicit statement, flag the gap.
- `implicit` — the report has relevant facts but doesn't state the answer; the answer requires inference. Lead with specialist data; use the report as supporting context (cite report excerpts as background, not as the answer). Flag stale-report risk if the report's narrative confidence outpaces what specialists could verify.
- `not_mentioned` — return team draft's answer, prepend exactly: `"No prior curated reports were found for this case — answer is from live specialist analysis only."`

## Flags (one line each)

- Report-vs-data disagreement — leading claim is the data-grounded one.
- Stale-report risk — confident-vs-data narrative mismatch even without direct contradiction.
- Open conflicts from `team_draft.open_conflicts`.
- Signal-bearing gaps (`team_draft.data_gaps` where `is_signal == true`).

Clean agreement, no conflicts, no signal-bearing gaps → `flags: []`.

## Data pull request

Emit `data_pull_request` when the combined evidence is materially incomplete — multiple `data_gaps` flagged `is_signal=true`, coverage `implicit` / `not_mentioned` plus residual gaps, or open conflicts unresolvable without more data.

Fields: `needed: bool`, `reason: str` (one sentence), `would_pull: [str]` (kinds of data that would help, phrased like `data_gaps`), `severity: low | medium | high`. Omit entirely OR set `needed: false` when no pull is warranted.

## Output

```json
{
  "answer": "1–3 paragraphs. On factual points, lead with specialist data; use the report for narrative context.",
  "flags": ["..."],
  "data_pull_request": { "needed": true, "reason": "...", "would_pull": ["..."], "severity": "..." }
}
```
