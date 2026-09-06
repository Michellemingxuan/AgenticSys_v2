---
name: Knowledge Base
description: Search the internal knowledge base of PRIOR case reports for cases that resemble this one, and return the matching patterns with their case_ids
type: helper
owner: [base_specialist]
mode: tool
tool_signature: "knowledge_base_search(question: str, target_pattern: str) -> str  # JSON"
inputs:
  question: str
  target_pattern: str
outputs:
  status: str
  similar_cases: list[str]
  answer: str
  retrieval_query: str
  patterns: list[dict]
  evidence: list[dict]
---

# What the knowledge base is

A corpus of PRIOR case reports, each distilled into ~10 points and then
clustered. Two kinds of cluster come out of that process:

- **common characteristics** — a pattern many cases share. Answers *"is this
  normal?"*, *"what usually happens before X?"*
- **unique characteristics** — a pattern that singles a case out. Answers
  *"what is unusual here?"*, *"who else looks odd in this same way?"*

Every characteristic carries the `case_id`s that exhibit it, so a match is a
list of real prior cases plus the distilled point (and its supporting quote)
that each of them contributed.

**It is about OTHER cases.** For what THIS case's specialists already found
this session, use `kp_lookup` / `kp_list_topics` (the knowledge-POINT cache) —
a different tool over different data. KB = other cases; KP = this case.

# When to call

- The reviewer asks for comparison to other cases: *"any other similar cases
  like this one?"*, *"have we seen this before?"*, *"is this pattern common?"*,
  *"what usually precedes a default like this?"*
- You have found something and want to know whether it is typical or singular
  before calling it a risk signal.

# When NOT to call

- The question is about THIS case's numbers. The KB holds prior-case narrative,
  not this case's data — it can never be the source of a figure about this
  customer.
- You have not yet established what this case actually shows. Retrieval quality
  is set by the `target_pattern` you name; run your own query first, then ask
  the KB about the pattern you found.

# The two arguments

- **`question`** — what is being asked, in the reviewer's terms. Your
  sub-question is usually it verbatim: *"any other similar cases like this
  one?"*, *"is this normal?"*
- **`target_pattern`** — the concrete behavioural pattern to retrieve on.
  This is the argument that decides what comes back.

# Framing `target_pattern` — pivot what "similar" means

The reviewer almost never says which axis of similarity they mean, and the
default reading ("cases like this one") retrieves nothing useful. Decide the
axis yourself, from the case in front of you:

1. Read what this case shows — your findings this turn, the KP cache, the
   conversation so far. (The tool also hands the recent turns to the KB when
   its client accepts a `conversation_history`, but do not rely on that: name
   the pattern explicitly, even on a follow-up like *"any others like that?"*.)
2. Pick the ONE behavioural pattern that makes this case what it is — the
   trajectory, the sequence, the anomaly — not a static attribute.
3. Ask for that pattern in plain language, with its shape and its outcome.

| Reviewer says | `target_pattern` |
|---|---|
| "any other similar cases?" (after a spend-spike finding) | "sharp merchant-concentrated spend spike in the 2-3 months before delinquency" |
| "has this happened before?" (after a score/bureau divergence) | "internal risk score deteriorating while bureau score stays healthy" |
| "is this normal?" (revolving balance) | "revolving balance held near limit for 6+ months with minimum-due-only payments" |
| "anything unusual about this case?" | ask twice — once for the common pattern the case fits, once for what singles it out |

If the first framing returns nothing, re-ask ONCE on a different axis (sequence
instead of magnitude, outcome instead of behaviour). Do not re-ask the same
question in different words.

# Output contract

```json
{
  "status": "ok",
  "similar_cases": ["case_123", "case_456"],
  "answer": "Grounded natural-language answer over the evidence below.",
  "retrieval_query": "persistent revolving balance leading to default",
  "search_text": "what was ACTUALLY searched — retrieval_query blended with
                  your target_pattern; present only when it differs",
  "patterns": [
    {"pattern_type": "common", "pattern": "Cluster summary text",
     "score": 0.786, "cases": ["case_123"]}
  ],
  "evidence": [
    {"case_id": "case_123", "pattern_type": "common", "point": "Bullet text",
     "why": "Why this point was written", "similarity": 0.79,
     "quote": "Original supporting quote"}
  ]
}
```

- `search_text` is `retrieval_query` blended with your `target_pattern`. When
  it appears and the results look thin, that is the string to judge — a weak
  search_text means the pattern was weak, and re-asking on another axis is
  worth the round.
- `status: "disabled"` with `"answer": "not applicable"` means prior-case
  comparison is switched OFF for this deployment (`knowledge_base.enabled` in
  `config/tuning.yaml`). Say "not applicable" and move on — it is a complete
  answer to the comparison, not a failure, so do not apologise for it or
  speculate about what the KB would have returned.
- `status: "unavailable"` with a `note` means the KB is ON but did not
  answer — a fault or a busy backend, not a setting. The note says which:
  a client that could not be loaded, a 429 rate limit its retries did not
  clear, or a timeout. Say so plainly and answer from this case's data.
- An empty `similar_cases` is a RESULT: "no comparable prior case was found for
  this framing" is a legitimate, useful answer.

# Using what comes back

- Cite `case_id`s and patterns as PRIOR-CASE context, clearly separated from
  this case's facts. Never let a KB figure become a number about this customer.
- **Never name a case the tool did not return.** An invented `case_id` is
  indistinguishable from a real referral to the reviewer reading it.
- `pattern_type` matters to the answer: "common" supports *"this is a familiar
  trajectory"*; "unique" supports *"this stands out, and here is who else did"*.
- **Scores rank, they do not certify.** Observed on real payloads: three
  clusters at 0.594 / 0.591 / 0.580, where the one that actually answered the
  question ranked LAST and the top one was off-topic — the KB's own answer
  said so. Read each `pattern` and judge it yourself. If none is on target,
  report that no close analogue was found rather than the best-scoring row.
- `answer` is the KB's own synthesis, not a verified finding. Ground claims in
  `evidence` (a `case_id` plus its `quote`) rather than relaying that prose.
- `excluded_self_case` means the case under review is itself in the corpus and
  was removed — it cannot be its own precedent. Prior-case ids are bare digit
  strings exactly like this case's, so always label which is which.

# Status

Every knob lives in `config/tuning.yaml` under `knowledge_base:` — `enabled`
is the master switch (`false` → the "not applicable" path above; unset →
on only when a client and a json_path are configured). An inline env var
still overrides the file.

The platform's client is
`answer_question(json_path, question, conversation_history, target_pattern)`,
resolved from the environment at call time — an installed package (`pkg.module:answer_question`) or a loose
script (`/abs/path/kb_answer.py:answer_question`) — via
`KNOWLEDGE_BASE_CLIENT` plus
`KNOWLEDGE_BASE_JSON=/abs/path/aggregated_rank_top_common_unique.json`. Its
arguments are bound by introspection, so it may declare any subset of
`json_path` / `question` / `conversation_history` / `target_pattern`. With
either variable unset — which is the case in dev — the tool reports
`unavailable` and never fabricates a match. Wiring the real client is a config
change, not a code change; callers do not change. See
`tools/knowledge_base.py`.

For a dev rehearsal, `tests/doubles/knowledge_base_sim.py` implements the same
signature over a small built-in corpus (case ids prefixed `sim_case_`, so a
simulated referral is never mistaken for a real one):
`KNOWLEDGE_BASE_CLIENT=tests.doubles.knowledge_base_sim:answer_question`.
