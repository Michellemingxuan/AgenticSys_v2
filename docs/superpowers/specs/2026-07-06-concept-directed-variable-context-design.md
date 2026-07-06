# Concept-directed variable context — design

**Date:** 2026-07-06
**Status:** Design approved in shape; awaiting spec review → implementation plan.
**Scope decision:** v1 targets the **modeling** specialist (`model_scores` + `model_scores_transaction`). The mechanism is generic but only *activates* for tables that carry `concept:` tags, so other domains are opt-in later by tagging their profiles.

---

## 1. Problem & goal

The `model_scores` table carries 50+ model variables (ratios, indices, out-of-pattern measures, third-party scores). To answer a sub-question, the modeling specialist must pick the *exactly correct* variable(s). Today it has two sources, and both are weak at selection:

- **The skill body** (`skills/domain/modeling.md`) contains a hand-written, **partial** "Concept → vocabulary lookup" (~15 of 50+ columns) plus curated semantic expertise. It drifts from the profiles (the recurring failure mode this repo keeps hitting) and doesn't cover every variable.
- **`get_table_schema('model_scores')`** dumps **all** columns present in the case as JSON (`type`, `description`, `canonical_name`, `aliases`, `declared_values`). The model must re-read ~50 descriptions and reason about which one matches — every time. Thresholds are buried in description *text* ("Values above 3.15 are risky"), not structured.

The rich, authoritative variable context (the numbered `context/*.txt` files) is **not read at runtime** — only the distilled profile descriptions are.

**Goal:** reliable variable selection — the specialist reliably lands on the right variable(s) for the sub-question's concept — while (a) keeping the knowledge **in sync** with the profiles (single source of truth) and (b) adding **no extra rounds**. Selection accuracy is the headline; sync is the strong second; "no extra round" is a hard constraint.

**Non-goals:** changing the orchestrator's routing to specialists; embedding/vector infra; reading `context/*.txt` at runtime; retiring `get_table_schema` (it remains the fallback and the source for `declared_values`).

---

## 2. Approach (chosen)

**Approach C (structured concept tags in the profiles) + orchestrator-directed dynamic assembly.**

The orchestrator — which already sees the whole question and routes to specialists — also **names the relevant concept(s)** when it calls a specialist. The profiles supply the variable details for those concepts. The specialist runner expands concept → variable slice and **injects it into the specialist's initial context**, next to the existing episodic + KB prepends. Because the slice is present in the first message, the specialist selects and queries in the same round it already uses — no extra round.

Separation of responsibilities:
- **Orchestrator** = semantic matching ("this sub-question is about *exposure-vs-remit / OOP*") — it has the whole question and a concept vocabulary in its prompt.
- **Profiles** = the single source of truth for each variable's concept, meaning, and threshold.
- **Runner** = mechanical expansion (concept → variables) + injection. No fuzzy matching.

### Approaches considered and rejected
- **Runner-side keyword match** of the sub-question against concept tags — rejected: reintroduces fuzzy matching; the orchestrator is a better matcher (sees the whole question).
- **Inject the full ~50-variable index into every modeling prompt** (static) — rejected: pays the token cost on every call regardless of relevance; the dynamic slice is strictly smaller.
- **A `lookup_variables(concept)` tool** — rejected: still a tool round, and it puts the matching burden back on the specialist mid-run instead of using the orchestrator's whole-question view.
- **LLM pre-pass to classify concepts** — rejected: adds a round/latency, violating the hard constraint.

---

## 3. Data model — single source of truth

Add an optional `concept:` field (string or list of strings) to each variable in:
- `config/data_profiles/model_scores.yaml`
- `config/data_profiles/model_scores_transaction.yaml`

Keep the existing structured `risk_threshold` / `risk_direction` fields; where a variable has a threshold only in its description text, promote it to structured fields as part of tagging.

**Concept taxonomy** (reuses the existing `modeling.md` vocab groups so nothing new is invented):
`internal_delinquency`, `external_delinquency`, `exposure_leverage`, `capacity_paydown`, `oop` (out-of-pattern / exposure-vs-remit), `spend_pattern`, `trends_tenure`, `bureau_derived`, `risk_events`, `output_score` (CDSS/TSR/clarity output ML scores), `third_party_score` (Paydex/SBFE/LN/RNN embedded scores).

A variable may carry more than one concept (e.g. `cust_expsr_avg_rem_12m_ratio` → `[oop, exposure_leverage]`).

Example:
```yaml
  cust_expsr_avg_rem_12m_ratio:
    dtype: float
    description: Customer Exposure / (Average Remit In 2-12 Months). Values above 3.15 are risky.
    risk_threshold: 3.15
    risk_direction: above
    concept: [oop, exposure_leverage]
    aliases:
    - cust_expsr_avg_rem_12m_ratio_max
```

**Non-breaking:** `concept:` is an unknown key to the generator (`datalayer/generator.py` reads only known keys per column) and to `get_table_schema` (reads `dtype`/`description`/`aliases`/`categories`). Both ignore it. A test asserts this.

---

## 4. Components

### 4.1 Concept index builder (`datalayer/catalog.py`)
A method on `DataCatalog`, e.g. `concept_index(table_or_tables) -> dict[str, list[VarCard]]`, returning `concept → [{name, description_short, threshold_text}]`, built from the profiles' `concept:` tags. `description_short` is the profile description trimmed to one line; `threshold_text` renders `risk_direction` + `risk_threshold` ("risky > 3.15") when present. Cached per catalog (profiles are stable within a session).

A second helper `concept_catalog(specialists) -> dict[str, str]` returns `concept → one-line gloss` for the orchestrator roster (derived from the same tags; the gloss is a short human label per concept, defined once in a small constant map keyed by the taxonomy above).

### 4.2 Orchestrator awareness (`agent_factories/orchestrator_agent.py`)
Extend `_render_team_roster(...)` so each specialist line also lists its **available concepts** (the distinct `concept:` values across its `data_hints` tables). This teaches the orchestrator the concept vocabulary it can direct with. Only specialists whose tables carry tags show concepts; others are unchanged.

### 4.3 Orchestrator direction — the `concepts` arg (`tools/redacting_tool.py`)
Add an optional parameter to the specialist tool:
```python
async def _runner(ctx: RunContextWrapper, sub_question: str, concepts: list[str] | None = None) -> str:
```
The `@function_tool` decorator surfaces `concepts` in the tool schema; the orchestrator sets it to the concept name(s) relevant to the sub-question. Optional — omitting it is valid and triggers the fallback.

### 4.4 Runner expansion + injection (`tools/redacting_tool.py`)
When `concepts` is non-empty, the runner resolves them against the catalog's `concept_index` for this specialist's `data_hints` tables, builds a compact **§ DIRECTED VARIABLES** block, and prepends it via the existing `_compose_specialist_input(...)` — which already prepends episodic + KB before the sub-question. Extend that helper to accept the directed-variable block as an additional prefix placed **last among the prefixes** (ordering: episodic, KB, directed-variables, then `--- New question ---`). Directed variables sit nearest the sub-question because they are the most question-specific and should be the most salient prefix.

Block shape (compact, one line per variable):
```
§ DIRECTED VARIABLES (for this question — from the data catalog)
[oop] cust_expsr_avg_rem_12m_ratio — Exposure ÷ avg remit (2–12mo); risky > 3.15
[oop] oop_interaction — Out-of-pattern spend index wrt exposure; risky > 28
[exposure_leverage] overall_exposure — Overall customer exposure (USD, monthly roll-up)
```

**Budget:** cap at the union of matched concepts, and a hard ceiling of N variables (default 15) to bound tokens; if a concept over-fills, keep the variables with structured thresholds first. Log the count injected.

### 4.5 Skill overlay (`skills/domain/modeling.md`)
The *mechanical* concept→variable map is now generated, so slim the hand-written "Concept → vocabulary lookup" table to a short pointer ("directed variables are injected per question; see §OOP for interpretation"). **Keep** the curated semantic overlay (OOP-is-the-pure-one, `oop_interaction`-is-adjusted, prefer-variable-over-recompute, exposure-vs-remit meaning) — that expertise is not derivable from tags and stays in the body.

---

## 5. Data flow

```
Orchestrator (has concept vocabulary in roster)
  │  calls specialist tool: sub_question + concepts=["oop"]
  ▼
redacting_tool._runner
  │  concepts → catalog.concept_index(model_scores, model_scores_transaction)
  │  → DIRECTED VARIABLES block (name · meaning · threshold)
  │  _compose_specialist_input(episodic, kb, directed_vars, sub_question)  # directed_vars nearest the question
  ▼
Specialist (R1): sees the right variables in-context
  │  → batch_summarize_trend / aggregate on the named variable(s)
  ▼
Specialist (R2): SpecialistOutput
```

No extra round versus today: the specialist's existing R1 (schema-probe folded into query) now often skips the schema probe because the directed variables are already present.

---

## 6. No-regression fallback

- `concepts` omitted, empty, or unmatched (no tagged variables for those concepts on this specialist's tables) → **inject nothing**; the specialist behaves exactly as today (reads `modeling.md`, calls `get_table_schema`).
- Tables without `concept:` tags → the mechanism is inert for that specialist.
- The injected block is *advisory*: the specialist may still call `get_table_schema` (e.g. for `declared_values` or to confirm a filter format). The block never removes a capability.

This makes the change **purely additive** — worst case equals current behavior.

---

## 7. Testing

**Unit — data model:**
- Generator loads the tagged profiles and generates `model_scores` unchanged (concept key ignored).
- `get_table_schema('model_scores')` output is unaffected by the new key.

**Unit — index builder:**
- `concept_index` over a fixture profile returns the expected `concept → variables` with trimmed descriptions and rendered thresholds.
- `concept_catalog` returns a gloss per concept present.

**Unit — runner injection:**
- `concepts=["oop"]` → the DIRECTED VARIABLES block contains `cust_expsr_avg_rem_12m_ratio` + `oop_interaction`, prepended before the sub-question.
- `concepts=None` / `[]` / `["nonexistent"]` → no block injected (byte-identical to today's `_compose_specialist_input` path). **No-regression assertion.**
- Budget cap: a concept with > N variables truncates to N, thresholded-first.

**Unit — orchestrator roster:**
- `_render_team_roster` lists the modeling specialist's concepts; specialists without tags show no concept line.

**Integration:**
- A directed modeling sub-question (`concepts=["oop"]`) runs end-to-end and the specialist's input contains the block (assert via the runner log field).

---

## 8. Files touched

| File | Change |
|---|---|
| `config/data_profiles/model_scores.yaml` | add `concept:` (+ promote inline thresholds to structured) per variable |
| `config/data_profiles/model_scores_transaction.yaml` | same |
| `datalayer/catalog.py` | `concept_index()` + `concept_catalog()` builders (cached) |
| `agent_factories/orchestrator_agent.py` | roster lists available concepts per specialist |
| `tools/redacting_tool.py` | `concepts` arg on `_runner`; `_compose_specialist_input` gains directed-variable prefix; expansion + budget + logging |
| `skills/domain/modeling.md` | slim mechanical vocab table to a pointer; keep semantic overlay |
| `tests/…` | unit + integration per §7 |

No gateway/loader changes: `DataCatalog` already auto-loads every profile YAML.

---

## 9. Open risks / notes

- **Orchestrator compliance:** it must populate `concepts` for the lift to fire. Mitigated by the roster teaching the vocabulary and by the fallback (omission just reverts to today). We do **not** hard-require it.
- **Tagging effort:** ~50 variables across two profiles need `concept:` tags. One-time; drives everything downstream. Tags should be grounded in the `context/*.txt` descriptions where available.
- **Token budget:** the injected slice is smaller than the full `get_table_schema` JSON it often replaces, and is capped at N. Net token effect is expected to be neutral-to-negative (i.e. a saving) on directed questions.
- **Taxonomy drift:** the concept gloss map (§4.1) is a small hand-maintained constant; keep it beside the taxonomy list and covered by a test that every tag used in a profile has a gloss.
