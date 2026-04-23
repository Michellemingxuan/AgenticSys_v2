---
name: Acropedia
description: Look up an abbreviation or term in the internal Acropedia knowledge base and return its full name + explanation
type: helper
owner: [chat_agent, guardrail_agent, base_specialist]
mode: tool
tool_signature: "acropedia_lookup(term: str) -> dict"
inputs:
  term: str
outputs:
  full_name: str
  explanation: str
---

# Purpose

Resolve abbreviations and jargon the reviewer may use. Acropedia is an internal platform — treat the term as user-provided and return the authoritative explanation.

# When to call

- Reviewer's question contains an abbreviation the LLM is uncertain about ("What's the DTI?", "Is FICO 8 the right model?", "What does WCC cover here?").
- A case report references a term that needs definition ("CBR score" in modeling.md).
- The Guardrail Agent wants to confirm a suspicious-looking term is actually a known domain term (not a prompt-injection attempt).

# When NOT to call

- The term is clearly a proper noun that is not a domain abbreviation ("what is Paris?").
- The term is case-specific data (an account number, a case-id, a merchant name) — Acropedia holds domain vocabulary, not case data.

# Output contract

Returns a dict:

```json
{
  "full_name": "Debt-To-Income Ratio",
  "explanation": "A ratio of monthly debt payments to gross monthly income; a common regulatory benchmark is 0.43 (43%)."
}
```

- If the term is not found, `full_name` equals the input term and `explanation` is a short "not available in Acropedia" note. Do not hallucinate a definition.

# Status

Backed by a stub adapter today (`tools/acropedia.py`) that returns canned entries for a small set of common terms. Swap in the real Acropedia client when the integration lands — callers do not change.
