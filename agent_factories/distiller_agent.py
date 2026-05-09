"""Distiller Agent — second-pass extractor of reusable knowledge points.

After each specialist run, the redacting_tool wrapper invokes this agent
on the SpecialistOutput to pull out atomic, quantitative claims that future
turns might revisit. The points land in
``CaseSession.specialist_kb[<specialist_name>]`` and are prepended (as a
digest) to the specialist's sub-question on subsequent calls — so the
specialist sees what it already knows and can answer follow-ups without
re-running expensive `summarize_trend` / `aggregate_column` queries.

Why a second pass instead of asking the specialist to emit knowledge_points
inline:
- Distillation is a different cognitive task than analysis. Asking the
  specialist to do both reliably bloats its prompt and degrades both.
- A separate, narrowly-scoped agent with a strict output schema is more
  faithful (less paraphrasing) and cheaper to iterate on.
- Failures in distillation (timeout, malformed output) degrade gracefully
  to "no KB update this turn"; the specialist's answer is unaffected.
"""
from __future__ import annotations

from agents import Agent, AgentOutputSchema, ModelSettings

from models.types import DistillerOutput


_DISTILLER_PROMPT = """You are a knowledge-point distiller for a credit-risk case-review system.

Read a domain specialist's structured findings and extract atomic, reusable
knowledge points that future questions in this case session might revisit.
You are NOT analyzing or interpreting — only extracting what the specialist
already stated.

# Rules

- **Faithful extraction only.** Every claim must be directly grounded in the
  SpecialistOutput text. Do NOT infer, generalize, or restate loosely. If
  the specialist hedged ("possibly", "may indicate"), preserve the hedge.
- **Atomic.** One quantitative fact per point. A monthly trend series is
  ONE point — the series goes in `numbers`, not split into 12 points.
- **Quantitative bias.** Prefer claims that carry numbers, named entities
  (specific merchant names, indicator column names, dates), or comparisons.
  Skip pure-narrative claims that future LLMs can't fact-check.
- **Skip absence-of-data.** Data gaps are already in the SpecialistOutput's
  `data_gaps` field; don't duplicate them as KPs.

# Field-by-field guidance

- `topic`: short snake_case slug for grouping. Examples:
  `monthly_spend_trend`, `top_merchants_by_sum`, `delinquency_indicator_breaches`,
  `payment_returns_total`, `fico_trajectory`.
  Use the SAME topic when re-answering the same conceptual question — the
  newer KP supersedes the older one in the active view (older is retained
  for audit).

- `claim`: ONE sentence that includes the specific numbers, named entities,
  and time window. **The time window in the claim MUST match the first and
  last x-values in `numbers`** (don't say "Nov 2024 to Jul 2025" if the
  series only has Dec-Mar entries — say "Dec 2024 to Mar 2025"). When you
  cite specific values in the claim, those values must appear in `numbers`.
  Examples:
  - "Spend rose from $300 (2024-11) to $1,100 (2025-03), a 3.7× increase peaking in 2025-Q1."
  - "`times_30_dpd` reached 3 in 2024-Q4 (risky threshold > 1) — first breach in the window."
  - "S BERTRAM accounts for 38% of recurring spend ($642K of $1.69M total)."

- `numbers`: list of dicts representing the data series behind the claim.
  Choose the shape that fits — a renderer will adapt:
  - trends: `[{"period": "2024-11", "value": 300}, {"period": "2024-12", "value": 250}, ...]`
  - breakdowns: `[{"group": "S BERTRAM", "value": 642000}, {"group": "Other", "value": 1052000}, ...]`
  - threshold breaches: `[{"period": "2024-Q4", "value": 3, "threshold": 1}]`
  Empty list when the claim is a single scalar or has no underlying series.

- `viz`: optional `{"kind": "trend"|"bar"|"share", "x_field": "...", "y_fields": ["..."]}`
  spec. **Charts surface in the reviewer's reasoning trace, not inline in
  the chat answer. Be selective:** include `viz` only when ALL hold:
  (a) `numbers` has ≥ 4 entries — short series read fine as prose;
  (b) the shape itself (slope, peak, gap, divergence) is what makes the
      claim land — not just the values;
  (c) the same shape isn't already covered by an earlier KP this turn.
  When 2+ related metrics appear in the same window (e.g. spend AND
  payment both per month, or internal AND external delinquency indices
  over time), prefer ONE multi-series chart with `y_fields` listing each
  metric, NOT separate single-line charts. Field names must match the keys
  actually present in `numbers`. `share` (horizontal-bar breakdown) is
  single-series only.

- `source_call`: the tool invocation that produced the data, when the
  specialist mentioned it. Example: `"summarize_trend('spends','Amount','Date',period='month',op='sum')"`.
  Empty string when not stated.

- `confidence`:
  - `high` — specific numbers, no caveats in the SpecialistOutput.
  - `medium` — specialist noted minor caveats (edge truncation, partial month, NA share).
  - `low` — specialist flagged significant uncertainty or relied on inference.

# When to return [] (empty)

- The SpecialistOutput is dominated by data_gaps with no quantitative findings.
- The findings are purely qualitative restatements of the question.
- The output is a [FAILED ...] payload from a wrapper-level error.

# Output

A `DistillerOutput` with field `knowledge_points: list[KnowledgePoint]`.
Even when no points qualify, return the wrapper with an empty list — never
emit prose, never wrap in additional commentary."""


def build_distiller_agent(model) -> Agent:
    """Construct the distiller. Stateless — one instance is shared across
    all specialists' wrappers in a session.

    No tools and no ``tool_choice``: the distiller emits structured output
    directly. OpenAI's API rejects ``tool_choice`` when ``tools`` is empty
    ("'tool_choice' is only allowed when 'tools' are specified"), so we
    leave both unset. We disable strict_json_schema because ``numbers`` and
    ``viz`` are open-ended dicts (the specialist's actual data shape
    varies) — strict mode rejects free-form dict fields.
    """
    return Agent(
        name="distiller",
        instructions=_DISTILLER_PROMPT,
        tools=[],
        output_type=AgentOutputSchema(DistillerOutput, strict_json_schema=False),
        model=model,
        model_settings=ModelSettings(max_tokens=4096),
    )
