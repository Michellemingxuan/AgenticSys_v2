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
  `payment_returns_total`, `fico_trajectory`, `cdss_score_trend`,
  `tsr_score_trend`.

  **Two KPs in the same turn MUST NOT share a topic unless they answer
  THE SAME conceptual question** (in which case the later supersedes the
  earlier in the active view; older is retained for audit). Different
  metrics, different columns, different score names = different topics.
  This rule is load-bearing: downstream chart collection dedupes by
  topic, so collisions cause charts to silently disappear.

  Examples that MUST get distinct topics (real failure case — both got
  `model_scores_trend` and CDSS's chart was overwritten by TSR's):
  - CDSS trajectory  → `cdss_score_trend` (column: `credit_loss_prob`)
  - TSR trajectory   → `tsr_score_trend`  (column: `tot_struct_risk_score`)
  - Internal delinq  → `internal_delinquency_index_trend`
  - External delinq  → `external_delinquency_index_trend`

  Naming rule: when the claim is about a specific named score / metric /
  indicator, **put the metric name IN the topic slug**, never a generic
  family label like `model_scores_trend` / `delinquency_trend` /
  `spend_metrics_trend`. If you're tempted to use a family name, ask
  whether the next KP this turn will share that family — if yes, you
  need per-metric slugs.

  **Preferred alternative when the question genuinely covers 2+ metrics
  on the same x-axis** (CDSS AND TSR over the same trans_month series):
  emit ONE multi-series KP instead of two single-series KPs. See the
  `viz` field below for the multi-series shape.

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

  **INCLUDE EVERY POINT FROM THE SOURCE SERIES — do NOT abridge.** If the
  specialist's `summarize_trend` or `summarize_by_group` returned N rows,
  `numbers` MUST contain all N entries, even when the `claim` text
  summarizes interior periods as "steady" / "flat" / "remained around X"
  or only names the anchor / peak / trough periods. The renderer plots
  `numbers` exactly as you provide; dropping intermediate rows produces a
  chart with gaps that misrepresents the data and contradicts the claim's
  time window. Example: a 9-month spend trend whose claim reads "starts
  at $224K (2024-11), remains steady through 2025-03, then drops to $19K
  (2025-07)" → `numbers` MUST list all 9 months (2024-11, 2024-12, 2025-01,
  …, 2025-07), NOT just the 3 anchor periods named in the claim.

- `viz`: optional `{"kind": "trend"|"trend_dual"|"trend_grid"|"bar"|"share", "x_field": "...", "y_fields": ["..."]}`
  spec. **Charts surface in the reviewer's reasoning trace, not inline in
  the chat answer. Be selective:** include `viz` only when ALL hold:
  (a) `numbers` has ≥ 4 entries — short series read fine as prose;
  (b) the shape itself (slope, peak, gap, divergence) is what makes the
      claim land — not just the values;
  (c) the same shape isn't already covered by an earlier KP this turn.

  **MULTI-METRIC RULE (load-bearing — read carefully).** When the
  specialist's findings cover 2+ related metrics on the SAME x-axis
  (typically a time series): emit ONE multi-series KP, NOT N single-
  series KPs. `numbers` becomes one row per period with one key per
  metric, and `y_fields` lists every metric to plot. Pick the kind by
  scale:
  - **Same scale and unit** (all percentages, all dollar amounts):
    `kind="trend"` with `y_fields=[var1, var2, ...]` — single shared
    y-axis, one line per variable.
  - **Exactly 2 metrics on different scales** (e.g. CDSS = 0-1
    probability + TSR = 0-100 score, or a count + a rate):
    `kind="trend_dual"` with `y_fields=[primary, secondary]` — twin
    y-axes.
  - **3-6 metrics on different scales**: `kind="trend_grid"` with
    `y_fields=[var1, var2, ...]` — stacked panels sharing the time axis.

  Worked example for the question *"how did CDSS and TSR react in this
  case?"* — these are different scales (probability vs. score band) so
  use `trend_dual`:
  ```
  topic = "cdss_tsr_trajectory"
  numbers = [
    {"period": "2024-11", "credit_loss_prob": 0.12, "tot_struct_risk_score": 24.5},
    {"period": "2024-12", "credit_loss_prob": 0.18, "tot_struct_risk_score": 28.2},
    ... ALL 18 trans_month entries from the specialist's series, no abridging ...
  ]
  viz  = {"kind": "trend_dual", "x_field": "period",
          "y_fields": ["credit_loss_prob", "tot_struct_risk_score"]}
  ```
  Two SEPARATE single-series KPs (`cdss_score_trend` and `tsr_score_trend`)
  is the WRONG shape here — the reviewer's mental model is "compare them
  side-by-side", not "show me two unrelated charts."

  Field names in `y_fields` must match the keys actually present in
  EVERY entry of `numbers`. `share` (horizontal-bar breakdown) is
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
