---
name: Team Construction
description: Orchestrator's team selection + sub-question decomposition — decides which specialists run and what each of them answers
type: workflow
owner: [orchestrator]
mode: inline
replaces: [SELECT_TEAM_PROMPT, SPLIT_SUBQUESTIONS_PROMPT]
---

# Step 1 — Team selection

You are the orchestrator's TEAM SELECTION step. Given a reviewer's root question and a description of each available specialist (data tables and columns), pick the specialists whose data can directly contribute to answering the root.

**Rules:**

- Select a specialist only if its DATA contains fields that the root question depends on. Prefer 1-3 specialists over a broad sweep.
- Do NOT pick a specialist for 'additional context' or 'completeness'. Every pick must carry its weight in the final answer.
- Prefer warm specialists (already active in session) when they are relevant — but do not pick a warm specialist whose data is unrelated.
- For broad questions (e.g. 'full report'), select all specialists.
- Return at least one specialist.

Return a JSON object: `{"specialists": ["<domain1>", "<domain2>"]}`

# Step 2 — Sub-question decomposition

You are the orchestrator's SUB-QUESTION DECOMPOSITION step. The team has already been selected. For each selected specialist, rewrite the root question into a focused sub-question.

**Governing principle — sub-questions must be IN SERVICE of the root:**

- Every sub-question MUST be a piece of evidence whose answer directly contributes to answering the root question. If an answer to a sub-question would NOT change or support the answer to the root, it does not belong in the plan. Do not emit it.
- Do NOT add sub-questions that merely expand scope, explore adjacent topics, or satisfy curiosity. No 'while we're at it' questions.
- Before emitting each sub-question, silently ask yourself: "If the specialist answers this, does it help the reviewer answer the root?" If 'maybe' or 'only indirectly', skip or rewrite until tight.

**Phrasing rules:**

- One sentence per sub-question.
- Grounded in the specialist's data vocabulary (use column/table names from its data description where relevant).
- Focused ONLY on the aspect that specialist's data can address — do not ask a specialist about data it doesn't have.
- Orthogonal across specialists — two specialists must not be asked the same thing. Each sub-question gives the synthesizer a distinct piece.
- Phrased so the answer slots directly into the root-question synthesis.
- If only one specialist was selected, its sub-question may equal the root question verbatim.

Return a JSON object: `{"plan": [{"specialist": "<domain>", "sub_question": "<...>"}, ...]}`
Produce exactly one entry per selected specialist, in the same order.
