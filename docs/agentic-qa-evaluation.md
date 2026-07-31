# Agentic Q&A evaluation protocol

Use two separate experiments. Mixing cold repeats with a warm conversation
turns consistency measurement into a cache test.

## 1. Cold quality and stability benchmark

Run each of roughly 10 representative questions 10 times in a fresh session:

```bash
python -m tests.test_consistency.evaluate \
  --suite tests/test_consistency/questions.json \
  --mode cold --k 10 --concurrency 1
```

Use concurrency 1 for clean latency comparison. Run a separate load test with
the production concurrency if capacity under load matters.

Headline measures:

- Team construction: modal exact-team rate and pairwise team Jaccard.
- Tool use: pairwise Jaccard over data tools used.
- Sub-question stability: token-Jaccard similarity aligned by specialist.
- Latency: median, p95, maximum, and Tukey 1.5×IQR outliers over the 10 repeats.
- Efficiency: prompt/completion/total tokens and number of leaf LLM calls.
- Reliability: percentage of turns with any retry, plus failed trace nodes.
- Content: configured deterministic checks, provenance completeness, and
  blinded human ratings.

Ten repeats are enough to expose obvious instability but not to estimate a
very precise p95. Report the observed p95 as a benchmark statistic, not as a
service-level guarantee.

## 2. Stateful memory benchmark

Arrange questions in the suite as short chains: seed question, exact repeat,
paraphrase, and genuine follow-up. Run them in one session:

```bash
python -m tests.test_consistency.evaluate \
  --suite tests/test_consistency/questions.evaluation.example.json \
  --mode stateful --k 3 --concurrency 1
```

Report three separate memory measures:

- QA-cache hit rate: full answer replay for exact or recognized near-duplicates.
- KB-context exposure rate: a specialist received distilled prior knowledge.
- KB-lookup hit rate: successful `kb_lookup` calls divided by all such calls.

Also compare warm versus cold tokens, LLM calls, latency, and answer quality.
A high hit rate is not automatically good if the answer becomes stale or the
question's entity/time window differs.

## Content quality

The specialist payload's `scope` and `measured_over` fields are valuable but
they prove only provenance and scope alignment. They do not prove that the
final synthesis is correct or complete.

Use four layers:

1. Outcome and routing: in-scope decision and required/allowed specialists.
2. Grounding: every data-specialist result has both `scope` and
   `measured_over`; requested table, population, metric, and date terms appear
   in that provenance.
3. Answer contract: required concepts are present and forbidden claims absent.
4. Blinded review: score correctness, completeness, relevance, clarity, and
   uncertainty calibration from 1–5. Reviewers should inspect the answer first,
   then open the separately generated `*_review_key.csv` to reveal
   scope/provenance and score scope correctness and unsupported claims.

The suite accepts an optional `evaluation` object per question:

```json
{
  "evaluation": {
    "expected_outcome": "ok",
    "required_specialists": ["spend_payments"],
    "allowed_specialists": ["spend_payments", "report_agent"],
    "required_scope_terms": ["payments", "2025"],
    "answer_must_include": ["returned"],
    "answer_must_include_any": [["decline", "decrease"]],
    "answer_must_not_include": ["unable to access"]
  }
}
```

After completing the blind sheet and revealing provenance with the key, build
the human content-quality summary:

```bash
python -m tests.test_consistency.score_reviews \
  tests/test_consistency/results/<run>_blind_review.csv \
  tests/test_consistency/results/<run>_review_key.csv
```

Before the full 100-run benchmark, run one question once and verify its trace
contains token counts. The SafeChain path may estimate tokens when provider
usage is unavailable; those figures are still useful for relative comparison
but should be labeled estimated.
