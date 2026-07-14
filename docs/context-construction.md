# How Context Is Constructed — Orchestrator & Specialist

This doc explains, with a worked example, exactly what goes into the **LLM
input** for the two agent roles in the system — the **orchestrator** and a
**specialist** — and which file builds each piece.

## Mental model: every agent's context = system prompt + user message

For both roles the context has two halves, built in two different places at two
different times:

- **System prompt** — the agent's *identity + playbook*. Built **once**, when
  the Agent object is constructed. Same for every call (until the per-round
  switch, below).
- **User message** — the *per-question input*. Built **per turn** (orchestrator)
  or **per tool call** (specialist), from compact side-channels.

The message list carries the conversation; everything else a tool needs
(gateway, KB, logger, …) rides on `AppContext` (`models/app_context.py`), passed
as `Runner.run(..., context=ctx)`.

## Side-by-side: what's in each, and where it's built

| | **Orchestrator** | **Specialist** |
|---|---|---|
| **System prompt — builder** | `agent_factories/orchestrator_agent.py` → `_compose_orchestrator_instructions` | `agent_factories/specialist_agent.py` → `_compose_instructions` |
| **System prompt — sections** | `§ PROTOCOL` (round rules + tool-use gate) · `§ SYNTHESIS` (`skills/workflow/synthesis.md`) · `§ R1 REFERENCE` (`team_construction.md` + `data_catalog.md`) · pillar concept-glossary · **TEAM ROSTER** (auto-generated from each specialist's domain `.md` × the data catalog) | `§ DOMAIN IDENTITY` (name · tables · interpretation guide · risk signals) · `§ DOMAIN EXPERTISE` (`skills/domain/<name>.md` body) · `§ PILLAR CONTEXT` (cut-off date · glossary · focus) · `§ WORKFLOW` (`skills/workflow/data_query.md`, the six-tool playbook) |
| **User message — builder** | `runner/turn/input_assembly.py` → `assemble_orchestrator_input(sess, verdict, ctx)` | `agent_factories/agent_tools/specialist_input_tool.py` → `assemble_specialist_input(...)` |
| **User message — pieces (in order)** | `[EPISODIC]` recent **whole turns** (coreference) · `[KB-warmth]` which specialists are warm · the **redacted question** | `[EPISODIC]` this specialist's **own** prior sub-answers · `[KB]` digest of its cached topic names (+ `kb_lookup` nudge) · `§ DIRECTED VARIABLES` (catalog-resolved columns for the passed `concepts`) · `--- New question ---` + the **sub-question** |
| **Built when** | once per **turn** (fresh graph each turn) | once per **tool call** (first call this turn; warm follow-ups reuse the stored transcript) |
| **Per-round system-prompt switch** | full → **synthesis** (drops `§ R1 REFERENCE`, ~23k→~10k chars) via `_dynamic_instructions`, keyed on `AppContext._domain_specialists_called` | full → **synthesis** (drops `§ DATA QUERY`/`§ CROSS-DOMAIN`) via `_dynamic_instructions`, keyed on a per-`ctx` call counter |
| **Driven by** | `TurnRunner` (`runner/turn/conductor.py`) → `Runner.run_streamed(orchestrator_agent, …)` | `agent_tool._runner` (`agent_factories/agent_tools/agent_tool.py`) → `Runner.run(specialist, …)` |
| **Output type** | `FinalAnswer` | `SpecialistOutput` |

## Worked example — a two-turn story

We follow **one case, two turns**, to see how turn 1's answer flows into turn
2's context. All strings below are **real rendered output** from the composers.

- **Turn 1 (already answered):** *"What is the FICO score trend for this case?"*
  → the orchestrator dispatched the **`bureau`** specialist → answer:
  *"FICO fell 712 → 648; steepest drop **Aug–Oct 2024**."* Two things were saved:
  the Q→A landed in `qa_cache` (→ episodic), and a distilled `fico_trend`
  KnowledgePoint landed in `bureau`'s KB.
- **Turn 2 (the new question):** *"**Did the internal model scores spike in
  those same months?**"* — note "those same months" is a coreference to turn 1.

The rest of this section walks **turn 2** through the four context pieces and
shows how the FICO finding threads through.

### Turn 2 · Orchestrator system prompt (built once, `_compose_orchestrator_instructions`)

Four sections; sizes for this roster:

| Section | Source | ~chars |
|---|---|---|
| `§ PROTOCOL` | hardcoded | ~1.1k |
| `§ SYNTHESIS` | `skills/workflow/synthesis.md` | ~7.4k |
| `§ R1 REFERENCE` | `team_construction.md` + `data_catalog.md` | ~13.4k |
| `TEAM ROSTER` | specialists × `DataCatalog` (live) | ~1–2k |
| **full (round 1)** | | **~23k** |
| **synthesis (round 2+)** | drops `§ R1 REFERENCE` | **~10k** |

The roster is what lets the orchestrator route *"model scores"* → the `modeling`
specialist, directed on the `output_score` concept (real, excerpt):

```
• bureau — Bureau domain skill — tradeline analysis, derog marks, score interpretation
    owns `bureau`: Credit bureau data — monthly case-level snapshots ...

• modeling — Modeling domain skill — `model_scores` carries output ML risk scores (CDSS, TSR) ...
    owns `model_scores`: Internal model scoring features — one row per scoring run per case.
    concepts you can direct: ... output_score (internal ML output scores (CDSS, TSR)); ...
    flags risks like: output ML risk score crosses catalog threshold; ...

ROUTING RULE: pick the specialist whose `owns` table most directly carries the reviewer's question.
When a specialist lists `concepts you can direct`, pass `concepts=[...]` naming the relevant concept(s).
```

### Turn 2 · Orchestrator user message (built per turn, `assemble_orchestrator_input`)

`[EPISODIC] ⊕ [KB-warmth] ⊕ redacted question`. Turn 1's FICO answer **and** its
`Aug–Oct 2024` window are visible here — that's how the orchestrator resolves
"those same months" (real):

```
[EPISODIC — recent turns this session, newest first. Use to resolve references ("it", "the second spike") and to avoid re-asking:
[{"turn_id": "turn-1", "question": "What is the FICO score trend for this case?", "sub_answers": [{"specialist": "bureau", "sub_question": "FICO score trend over the window", "sub_answer": "fico_score 712 -> 648 (-64); steepest drop Aug-Oct 2024."}], "final_answer": "FICO fell from 712 to 648 over the 18-month window; steepest drop Aug-Oct 2024."}]
]

[KB-warmth — cached specialist knowledge from prior turns. Use topic details to anchor sub-questions and avoid redundant queries:
  bureau (2 KPs):
    - fico_trend: fico_score declined 712 -> 648 over 18 months, steepest Aug-Oct 2024
    - derogs: derog_count rose from 1 to 4 in the final quarter
Reuse warm specialists for in-domain follow-ups. Reference specific cached findings in sub-questions when relevant.]

Did the internal model scores spike in those same months?
```

(On a *first* turn both prefixes are empty and the message is just the bare
question — the scaffolding only appears once there's history to carry.)

### Turn 2 · Orchestrator routing decision — the hinge of the story

Reading the episodic window (`Aug–Oct 2024`) + the roster (`modeling` owns
`model_scores`, directable on `output_score`), the orchestrator's LLM emits a
tool call that **folds turn 1's window into the sub-question**:

```
modeling(
  concepts=["output_score"],
  sub_question="Did the internal model scores (CDSS / credit_loss_prob) rise
                in Aug-Oct 2024 — the months FICO fell?"
)
```

### Turn 2 · Modeling specialist system prompt (built once, `_compose_instructions`)

`§ DOMAIN IDENTITY` → `§ DOMAIN EXPERTISE` (the `modeling` domain `.md`) →
`§ PILLAR CONTEXT` → `§ WORKFLOW` (`data_query.md`). Unchanged by the question —
it's `modeling`'s identity + playbook.

### Turn 2 · Modeling specialist user message (built per call, `assemble_specialist_input`)

`modeling` has **never run this session**, so its *own* episodic slice and *own*
KB block are empty — but it still (a) sees `bureau`'s FICO finding via the
**cross-specialist KB pointer** (`kb_lookup`), (b) gets the exact `output_score`
columns + thresholds from **directed variables**, and (c) receives the
window-anchored sub-question (real):

```
[KB — other specialists' cached topics (use kb_lookup(topic) to retrieve without re-querying):
  bureau: fico_trend, derogs]
Call kb_lookup(topic) to get cached data before re-querying. Call kb_list_topics() to see all cached claims.

§ DIRECTED VARIABLES (for this question — from the data catalog)
[output_score] credit_loss_prob — ML model score predicting likelihood of default in next 18 months; risky > 10
[output_score] tot_struct_risk_score — ML model score predicting likelihood of default on internal/external trades in next 18 months; risky > 20

--- New question ---
Did the internal model scores (CDSS / credit_loss_prob) rise in Aug-Oct 2024 - the months FICO fell?
```

### Turn 2 · Modeling specialist — round 1 → round 2 (the item list grows)

That user message is `modeling`'s **round-1** input. Because
`tool_choice="required"`, round 1 must call a data tool; the SDK runs it and
**appends the call + its output** to the item list, so **round 2** sees the
data. Below, the message *shapes* are the real SDK item format (via
`to_input_item()`) and the tool output is a real `summarize_trend` payload
shape; the values are for this scenario.

**Round 1** — `system` = full playbook; `messages` = `[the user message above]`.
`tool_choice="required"` → the model emits a data-tool call:

```
assistant → function_call  summarize_trend(table="model_scores",
              value_column="credit_loss_prob", date_column="month", period="month")
```

**The SDK executes the tool and appends two items** to `generated_items` — these
get replayed as input next round (`agents/run.py:1244`):

```jsonc
// 1) the assistant's tool call
{ "type": "function_call", "call_id": "call_a1", "name": "summarize_trend",
  "arguments": "{\"table\":\"model_scores\",\"value_column\":\"credit_loss_prob\",\"date_column\":\"month\",\"period\":\"month\"}" }

// 2) the tool result  (output is carried as a JSON *string*; shown pretty here)
{ "type": "function_call_output", "call_id": "call_a1", "output": {
    "table": "model_scores", "period": "month", "op": "mean",
    "value_column": "credit_loss_prob",
    "summary": {
      "n_buckets": 18,
      "first":  {"period": "2023-11", "value": 6.2},
      "last":   {"period": "2025-04", "value": 14.1},
      "peak":   {"period": "2024-10", "value": 15.3},
      "trough": {"period": "2023-11", "value": 6.2},
      "pct_change_first_to_last": "+127.4%",
      "slope_per_bucket": "0.52", "missing_periods": [] },
    "series": [ /* per-month credit_loss_prob … */ ] } }
```

**Round 2** — `system` switches to the **synthesis** prompt (`§ DATA QUERY`
dropped), `tool_choice` is now `"auto"`, and `messages` =
`[user message, function_call, function_call_output]`. The model now has the
trend (peak 15.3 in **2024-10**, right in the FICO window) and emits its
`SpecialistOutput`:

```jsonc
{ "domain": "modeling", "mode": "chat",
  "findings": "Yes — credit_loss_prob rose from ~6 to a 15.3 peak in Oct 2024 (risky > 10),
               climbing through Aug-Oct 2024 in step with the FICO drop; +127% over the window.",
  "evidence": ["credit_loss_prob peak 15.3 @ 2024-10 (threshold 10)",
               "pct_change +127.4% first→last"],
  "data_gaps": [] }
```

Because this response has **no tool calls** and matches the `SpecialistOutput`
schema, the SDK sets `next_step = NextStepFinalOutput` (`agents/run.py:594`) and
the specialist's loop ends. `agent_tool._runner` then redacts this payload and
returns it to the orchestrator as the specialist's tool output.

### The thread, end to end

```
Turn 1  bureau answers "FICO 712→648, steepest Aug-Oct 2024"
          │  saved to → qa_cache (episodic)  +  bureau KB (fico_trend)
          ▼
Turn 2  orchestrator user message  = [EPISODIC turn-1 FICO] + [KB-warmth bureau] + "…those same months?"
          │  resolves "those same months" = Aug-Oct 2024  (from episodic)
          │  routes to modeling, concepts=[output_score]   (from roster)
          ▼
        modeling user message  = [KB: bureau fico_trend]  +  §DIRECTED VARIABLES(output_score)
                                 +  "…rise in Aug-Oct 2024 — the months FICO fell?"
```

One turn-1 finding (FICO's Aug–Oct 2024 drop) reaches the turn-2 specialist
**three** ways: as episodic context the orchestrator used to anchor the window,
as a cross-specialist KB pointer the specialist can `kb_lookup`, and as the
window folded into its sub-question — without ever re-running `bureau`.

## Per-round dynamics (why the context changes each round)

Both agents run the SDK's agent loop; within one run, each round's context
differs via two mechanisms:

| Mechanism | What changes | Where |
|---|---|---|
| **Item-list growth** | each round's input = original user message + every item produced so far (assistant tool-calls + **tool outputs**) | SDK `agents/run.py:1243-1244` (`input = original_input + [i.to_input_item() for i in generated_items]`); accumulated by the loop at `run.py:509`/`:588` |
| **System-prompt switch** | round 1 = full playbook; round 2+ = lean synthesis prompt | `_dynamic_instructions` — orchestrator `orchestrator_agent.py`, specialist `specialist_agent.py:102`; the SDK calls `get_system_prompt` per round (`run.py:1237`) |
| **`tool_choice` flip** | round 1 forced to call tools (`"required"`); round 2 free to emit output (`"auto"` via `reset_tool_choice`) | Layer-0 `ModelSettings` in each factory |

The loop continues vs. stops based on the model's output: tool calls →
`NextStepRunAgain` (loop); a schema-matching final output with no pending tool
calls → `NextStepFinalOutput` (stop). Dispatched at `agents/run.py:594` /
`:621`; bounded by `max_turns`.

## Cross-turn continuity (why turns can stay stateless)

The message list is **not** accumulated across turns. Continuity is carried by
three compact side-channels re-attached each turn:

| Channel | Holds | Feeds |
|---|---|---|
| `qa_cache` → episodic | recent Q→A pairs | both agents' `[EPISODIC]` blocks |
| `AppContext._specialist_kb` (aliases `CaseSession.specialist_kb`) | distilled KnowledgePoints, **persists across turns by reference** | orchestrator `[KB-warmth]`, specialist `[KB digest]`, `kb_lookup` |
| `_specialist_histories` | a specialist's within-turn transcript | warm follow-up calls (per turn only) |

## File reference

| Concern | File | Symbol |
|---|---|---|
| Orchestrator system prompt | `agent_factories/orchestrator_agent.py` | `_compose_orchestrator_instructions`, `_render_team_roster`, `_dynamic_instructions` |
| Orchestrator user message | `runner/turn/input_assembly.py` | `assemble_orchestrator_input` |
| Orchestrator drive | `runner/turn/conductor.py` | `TurnRunner._run_orchestrator` |
| Specialist system prompt | `agent_factories/specialist_agent.py` | `_compose_instructions`, `_dynamic_instructions` |
| Specialist user message | `agent_factories/agent_tools/specialist_input_tool.py` | `assemble_specialist_input`, `_compose_specialist_input`, `_render_directed_variables` |
| Specialist drive (wrapper) | `agent_factories/agent_tools/agent_tool.py` | `_runner` |
| Shared runtime context | `models/app_context.py` | `AppContext` |
| SDK loop (per-round input) | `agents/run.py` (dependency) | `_run_single_turn` (`:1243-1244`), loop (`:509`) |
