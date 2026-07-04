# Episodic Context Injection — Design Spec

Date: 2026-07-05
Status: Draft (for review)
Owner: server / orchestration

## 1. Problem

The system deliberately keeps **no conversation history** (server.py: "each turn
starts fresh"). Cross-turn memory today is the **KB** (distilled topics + numbers,
via a warmth hint + `kb_lookup`) and the exact-match **`qa_cache`** (full-answer
replay for identical/near-duplicate questions). Neither supports **coreference /
follow-up resolution**:

> User: *"How did CDSS react?"* → then *"When did **it** reach the **second spike**?"*

To resolve *"it"* → CDSS and *"the second spike"* → the one established last turn,
the orchestrator must see the **immediate conversational thread**. The KB can't:
its warmth hint says a `cdss_score_trend` topic *exists*, but not that the user
*just asked about CDSS* — and coreference is implicit, so the model can't know to
`kb_lookup` a referent it hasn't resolved yet. The thread must be **present in
context (pushed)**, not pull-on-demand.

## 2. Goals / Non-goals

**Goals**
- Add an **episodic tier**: recent turns' structured `question → sub-answers →
  final answer`, pushed into context so the orchestrator (and each specialist)
  can resolve references and maintain continuity.
- Source it from **`qa_cache`** (it already stores the needed data) — no parallel
  store.
- Constant per-turn context cost regardless of session length.

**Non-goals**
- No change to `qa_cache`'s exact/near-duplicate replay behavior.
- No change to the KB (warmth hint stays all-topics; `kb_lookup` unchanged).
- No relevance-ranked selection yet — recency now, behind a pluggable selector.
- No cross-specialist leakage into specialist context (each specialist sees only
  its OWN prior sub-Q/sub-A).

## 3. The episodic record

Parsed per turn into:
```json
{
  "turn_id": "4bcb9a660e85",
  "question": "Did the spending spike, what drives it?",
  "sub_answers": [
    {"specialist": "spend_payments", "sub_question": "...", "sub_answer": "Spend peaked $404K May 2025; S BERTRAM 34%."},
    {"specialist": "modeling",       "sub_question": "...", "sub_answer": "TSR 39.6 in 2024-09; oop_interaction peaked 2024-05."}
  ],
  "final_answer": "Yes — spend spiked to $404K in May 2025..."
}
```
- `sub_answer` = the specialist's **`findings`** (or `report_agent`'s `answer`),
  parsed out of the stored `payload` (see §5). Not the evidence list.
- `final_answer` is truncated to `EPISODIC_ANSWER_CHARS` (default 800) — enough to
  ground references, bounded so 3 records stay small.
- Format is **JSON** at both injection points.

## 4. Source: `qa_cache` + a `turn_seq` stamp

Each `qa_cache` entry already carries the record's raw material: `origin_question`
(→ `question`), `tool_calls[].sub_question` + `tool_calls[].payload` (→ `sub_answers`),
and `answer` (→ `final_answer`). So `qa_cache` is the single source — no separate
`episodic_log`.

**Wrinkle (must fix):** `qa_cache` is keyed by question and **LRU-reordered**
(`_get_cached_qa` pops+reinserts on a hit), so dict order ≠ turn order. "Last 3 in
the dict" would be last-3-*accessed*, not last-3-*turns*.

**Fix:** at store time (`_store_cached_qa`), stamp each entry with a monotonic
**`turn_seq: int`** (a per-session counter incremented per stored turn). Selection
orders by `turn_seq` descending — immune to LRU reordering. (Existing entries
without the field sort last / are treated as oldest.)

Notes:
- A cache **hit** replays an existing entry and does **not** create a new one, so
  the episodic thread reflects **distinct prior questions** in turn order — fine
  for coreference (a verbatim repeat adds no new referent).
- `turn_seq` is also cleared/rewound with `qa_cache` (no separate lifecycle).

## 5. Parsing the sub-answer from `payload`

The stored `payload` is either a SpecialistOutput —
`'[Sub-question: ...]\n{"domain":"modeling","findings":"...","evidence":[...]}'` —
or, for `report_agent`, a ReportDraft `{"coverage":"...","answer":"...",...}`.
Parser: strip a leading `[Sub-question: ...]\n` prefix if present, `json.loads`
the remainder, take **`findings` if present, else `answer`** (covers both shapes).
`report_agent` **IS included** (its `answer` is its sub-answer). Failure handling
(never raise):
- Non-JSON / neither `findings` nor `answer` / a `[FAILED ...]` sentinel → skip
  that sub-answer (omit it from `sub_answers`).
- Truncate the extracted `sub_answer` to `EPISODIC_SUBANSWER_CHARS` (default 400):
  `findings` are already short, but a ReportDraft `answer` can be long.

## 6. Selection

Two pluggable selectors over the parsed records, both v1=recency (sort by
`turn_seq` desc), swappable to relevance-ranked later without touching call sites:

- **`select_episodic(records, k=3)`** — the last `k` **whole** turn records
  (newest first). For the ORCHESTRATOR.
- **`select_specialist_episodic(records, specialist, k=3)`** — this specialist's
  OWN last `k` `{sub_question, sub_answer}` pairs — i.e. filter every record's
  `sub_answers` to `specialist == name` FIRST, then take the newest `k`. So a
  specialist that last ran 5 turns ago still sees its most recent answers (its
  continuity), not an empty slice. For the SPECIALIST.

To feed the per-specialist selector, `server.py` threads a **bounded window** of
recent records (last `EPISODIC_WINDOW`, default 10) into `AppContext` — large
enough to contain each specialist's last-3. `k` = `EPISODIC_TURNS` (default 3);
window = `EPISODIC_WINDOW` (default 10); both env-tunable.

## 7. Injection points

**7a. Orchestrator** (server.py, where `framed_question` is built alongside the
KB-warmth hint): prepend an `[EPISODIC — recent turns]` block = the last 3 **full**
records (JSON array). Order in the framed input:
`[EPISODIC …] + [KB-warmth …] + <question>`. The episodic block gives coreference +
routing; the KB hint (all topics) + `kb_lookup` give facts.

**7b. Specialist** (`tools/redacting_tool.py`, `_runner`'s `contextual_in`
construction, on the first call of a turn — same place the KB digest is prepended):
prepend an `[EPISODIC — your recent answers]` block = `select_specialist_episodic(
records, name, k=3)` — **this specialist's own** last-3 `{sub_question,
sub_answer}` pairs (§6), which may reach further back than the global recent-3.
Order: `[EPISODIC slice] + [KB digest] + <sub-question>`. No other specialist's
thread is shown.

**Threading the data:** `server.py` builds the parsed record window once per turn
(the last `EPISODIC_WINDOW` records) and passes it into `AppContext` (a new
`_episodic_records` field, by value — like `_specialist_kb` is threaded), so
`redacting_tool._runner` runs `select_specialist_episodic` per specialist without
re-reading `qa_cache`. The orchestrator block uses `select_episodic(window, 3)`.

## 8. Division of labor (with the KB)

| | Carries | For | Bound |
|---|---|---|---|
| **Episodic slice** | raw recent `sub_question`/`sub_answer` (findings) | coreference + continuity | last 3 (rankable) |
| **KB** | distilled topics + numbers | fact reuse via `kb_lookup` | all topic names pushed + pull |

They are two views of the same history; pushing both is intentional and bounded
(episodic = recent-3 raw; KB = compact topic names). No `kb_lookup` change.

## 9. Bounding / cost

- Orchestrator: +3 records (each ≈ question + a few ≤50-word findings + one final
  answer). Bounded, constant per turn.
- Specialist: +3 of its own `{sub_question, findings}` pairs — small.
- Independent of session length (selector caps at `k`), so no growth as
  `qa_cache` fills to 64.

## 10. Edge cases / error handling

- **Empty / <3 entries:** inject what exists (0–2 records); omit the block if none.
- **First turn:** no `qa_cache` → no episodic block (identical to today).
- **Parse failure on a sub-answer:** omit that sub-answer; never raise (the turn
  must not fail on episodic assembly — wrap in try/except, log `episodic_*`).
- **The current question is itself a cache hit:** the replay path (server.py:1066)
  short-circuits before injection — episodic injection only runs on the fresh path.
- **Near-duplicate current turn:** same as a normal fresh turn if it doesn't hit;
  the record it produces stamps a new `turn_seq`.

## 11. Testing

- `select_episodic`: returns newest-3 whole records by `turn_seq` (not dict/LRU
  order); caps at `k`; handles <3 and empty.
- `select_specialist_episodic`: returns this specialist's OWN newest-3
  `{sub_question, sub_answer}` (filter-then-take-k) — including the case where the
  specialist did NOT run in the global recent-3 turns but ran earlier (still
  returns its earlier answers, not empty); other specialists' threads absent.
- sub-answer parser: strips `[Sub-question: …]` prefix + extracts `findings` else
  `answer` (so `report_agent`'s `answer` is included); skips non-JSON / `[FAILED …]`
  / neither-field; truncates to `EPISODIC_SUBANSWER_CHARS`.
- `final_answer` truncation to `EPISODIC_ANSWER_CHARS`.
- Record assembly: a `qa_cache` entry → the §3 JSON shape.
- Injection: orchestrator `framed_question` contains the episodic block +
  KB-warmth hint + question (order); specialist `contextual_in` contains its own
  slice + KB digest; cache-hit path injects nothing.
- Bounding: with 10 stored turns, exactly 3 records injected.

## 12. Files touched (anticipated)

- `server.py` — `turn_seq` stamp in `_store_cached_qa`; a per-session counter on
  `CaseSession`; `select_episodic` + the `findings` parser (or import from a
  small `tools/episodic.py`); prepend the orchestrator episodic block; thread
  `_episodic_records` into `AppContext`.
- `agent_factories/app_context.py` — `_episodic_records` field.
- `tools/redacting_tool.py` — prepend the specialist episodic slice in `_runner`
  (note: this file was relocated from `agent_factories/` in the redacting_tool
  decomposition — build on that).
- Optional new `tools/episodic.py` — `select_episodic`, the parser, record
  assembly (keeps server.py lean; testable in isolation).
- Tests per §11.

## 13. Dependencies / sequencing

- Builds on `tools/redacting_tool.py` (relocation PR) for §7b, and touches
  `server.py`'s orchestrator-framing + `qa_cache` code — which the plan-review
  dispatch PR also modifies. Base this work on whichever of those has landed;
  if both are open, base on the one carrying the needed `server.py`/`redacting_tool`
  state and reconcile at merge. (Design is independent; only line-level placement
  depends on them.)

## 14. Open questions / risks

1. **`turn_seq` monotonicity across rewind:** the counter should not reuse values
   after a partial rewind (which drops some entries) — keep it strictly
   increasing per session; dropped entries just leave gaps.
2. *(Resolved)* **Token budget:** `final_answer` capped at `EPISODIC_ANSWER_CHARS`
   (default 800) and each `sub_answer` at `EPISODIC_SUBANSWER_CHARS` (default 400).
   Generous caps — not expected to bind in practice, just a guard against a
   runaway long answer bloating the prompt.
3. *(Resolved)* **`report_agent` IS included** in `sub_answers` (its `answer` field
   is its sub-answer).
