# Orchestrator + Skills Refactor — Design Spec

**Date:** 2026-04-23
**Snapshot of current state:** `brainstorm/2026-04-23-architecture-current.md`
**Visualization companion (already shipped):** `brainstorm/architecture-v8.html`

## Goal

Reshape the per-question pipeline so that:

1. Pre-existing curated case reports under `results/<case-id>/*.md` are consulted as a primary answer source, in parallel with the existing team-construction workflow, and merged by a **Balancing skill** the user can iterate on as a markdown file.
2. Every prompt that today lives inline in Python (`SELECT_TEAM_PROMPT`, `SPLIT_SUBQUESTIONS_PROMPT`, `SYNTHESIZE_PROMPT`, `COMPARE_SYSTEM_PROMPT`, `BASE_INSTRUCTIONS`, the per-domain `system_prompt`s) becomes a versioned **markdown skill file** under `skills/{workflow,domain,helper}/*.md` with YAML frontmatter for structured fields.
3. Two new boundary agents — **Guardrail Agent** (input side) and **Data Manager Agent** (data side, renamed from Data Gatekeeper) — get explicit homes for redaction and topic-gating.
4. Orchestration moves from imperative async Python to a **LangGraph `StateGraph`** with **LangChain `ChatOpenAI`** (or equivalent) under the hood, and the existing `FirewallStack` is preserved as middleware around every LLM call and every inter-agent transit.

The system stays runnable at every phase boundary; nothing is a big-bang rewrite.

## Non-goals

- Not implementing real Lumi / central-DB integration — current `SimulatedDataGateway` stays.
- Not building the Acropedia platform — the helper skill assumes an internal API and stubs it.
- Not changing the data generator, pillar configs, or per-case case-id firewall handling.
- Not adding new domain expertise — the seven existing domains carry over verbatim.
- Not adding streaming / checkpointing yet (LangGraph supports them; future work).

## §1 — Architecture overview

### Per-question control flow

```
Reviewer question
   │
   ▼
Guardrail Agent              (redact + relevance_check)
   │ (pass)                  rejects off-topic upstream
   ▼
Chat Agent                   (converse skill)
   │
   ▼
Orchestrator                 (team_construction + synthesis + balancing + data_catalog)
   │
   ├──── parallel ────┐
   ▼                  ▼
Report Agent          Team workflow
  needle              ├─ Base Specialists ×N
  analysis            │    domain · data_query · data_analysis
                      │    (data flows through Data Manager: redact + data_catalog)
                      ├─ General Specialist
                      │    comparison · conflict_solver
                      └─ Synthesis (orchestrator)
   │                  │
   └──── merge ───────┘
   ▼
Balancing skill (orchestrator)
   ▼
Final answer back through Chat Agent → Reviewer
```

### Branching policy (lives in the Balancing skill, not in Python)

The orchestrator **always** invokes the Balancing skill with both drafts. Python never branches on coverage. The skill's markdown body decides:

- `coverage == "full"` → present report's answer; cross-check team draft. If team confirms, reinforce. If team contradicts on a specific point, flag the report issue inline.
- `coverage == "partial"` → lead with report on points it covers, supplement with team findings on uncovered points; same flag-discrepancy rule.
- `coverage == "none"` → return team draft verbatim, prepend a one-line note that no relevant prior reports were found.

This keeps the Python control flow trivial and lets non-engineers tune the merge policy by editing the markdown.

### Firewall as bus

`FirewallStack` becomes the chokepoint for **both** LLM calls (existing `firewall.call`) **and** every inter-agent transit (new `firewall.send`/edge middleware). Every cross-agent edge in the diagram above runs through the firewall: log the transit, apply the redact policy from `workflow/redact.md`, validate the message shape via Pydantic.

## §2 — Skill inventory & ownership

**22 skills total** = 13 workflow + 7 domain + 2 helper. Layout: `skills/{workflow,domain,helper}/*.md`.

**Agent roster (7):**

| Agent | Owned skills | Notes |
|---|---|---|
| Guardrail Agent (new) | relevance_check, redact (shared) | Input boundary; rejects off-topic |
| Chat Agent | converse + helpers (web_browser, acropedia) on demand | LLM-decided helper invocation |
| Orchestrator | team_construction, synthesis, balancing, data_catalog (shared) | Routes the parallel paths |
| Report Agent (new) | report_needle, report_analysis | Consumes `results/<case-id>/*.md` |
| Base Specialist (×N) | 1-of-7 domain, data_query, data_analysis | Recruited per question; inner chain inline |
| General Specialist | comparison, conflict_solver | Pairwise review |
| Data Manager Agent (new, renamed from Data Gatekeeper) | redact (shared), data_catalog (shared) | Sits between specialists and the gateway |

**Skill files:**

| File | Owner | Mode | Purpose |
|---|---|---|---|
| `workflow/converse.md` | Chat Agent | inline | Reviewer-facing tone + scope rules for `/chat` follow-ups |
| `workflow/relevance_check.md` | Guardrail Agent | inline | Decide if the question is in-scope for case review; reject off-topic |
| `workflow/report_needle.md` | Report Agent | inline | Locate relevant `.md` in `results/<case-id>/`; return list + relevance hints + `coverage` flag |
| `workflow/report_analysis.md` | Report Agent | inline | Read selected files, extract evidence excerpts → ReportDraft |
| `workflow/team_construction.md` | Orchestrator | inline | Replaces `SELECT_TEAM_PROMPT` + `SPLIT_SUBQUESTIONS_PROMPT` |
| `workflow/synthesis.md` | Orchestrator | inline | Replaces `SYNTHESIZE_PROMPT`; merges N specialist outputs + review → TeamDraft |
| `workflow/balancing.md` | Orchestrator | inline | New. Merges ReportDraft + TeamDraft per the §1 policy |
| `workflow/data_query.md` | Base Specialist | inline | Text-to-SQL-style — pick table + columns, call `query_table` |
| `workflow/data_analysis.md` | Base Specialist | inline | Given question + returned rows, produce insights/answer |
| `workflow/comparison.md` | General Specialist | inline | Pairwise comparison logic |
| `workflow/conflict_solver.md` | General Specialist | inline | Identify → raise → self-answer → resolve/escalate loop |
| `workflow/redact.md` | Shared (Guardrail + Data Manager) | inline | Identifier redaction; same logic, two consumers |
| `workflow/data_catalog.md` | Shared (Orchestrator + Data Manager) | inline | Catalog query patterns; how to interpret tables/columns |
| `domain/{bureau,crossbu,modeling,spend_payments,wcc,customer_rel,capacity_afford}.md` | Base Specialist (1 injected per recruit) | inline | Domain knowledge + thresholds + signals; frontmatter carries `data_hints`, `risk_signals`, `decision_focus`, `prompt_overlay` |
| `helper/web_browser.md` | floating (tool-callable) | tool | Placeholder body for now |
| `helper/acropedia.md` | floating (tool-callable) | tool | Real adapter to internal Acropedia platform; stub if API not yet available |

**Sharing:** `workflow/redact.md` is loaded by both Guardrail and Data Manager; `workflow/data_catalog.md` is loaded by both Orchestrator and Data Manager. Markdown is just files — multiple agents can inline-inject the same one with no duplication.

## §3 — Markdown skill format

Single loader (`skills/loader.py`) reads any `*.md` under `skills/`, parses YAML frontmatter, returns a `Skill` object: `Skill(name, description, type, owner, mode, body, meta)`.

### Common frontmatter (all skills)

```yaml
---
name: Report Needle              # human-readable
description: ...                 # one line
type: workflow                   # workflow | domain | helper
owner: [report_agent]            # list — supports shared skills
mode: inline                     # inline | tool
---
```

### Workflow skills add

```yaml
inputs: { question: str, case_folder: path }
outputs: { coverage: "full|partial|none", files: list, hints: list }
tools: [list_files, read_file]   # tools the LLM may call from this skill
```

### Domain skills add (preserves the current `DomainSkill` fields verbatim)

```yaml
data_hints: [model_scores, score_drivers]
risk_signals: ["PD jump >0.05", "drift >2σ"]
decision_focus: "default risk score and drivers"
prompt_overlay:
  credit_risk: "Anchor to cut-off; …"
```

### Helper skills add

```yaml
tool_signature: "acropedia_lookup(term: str) -> {full_name: str, explanation: str}"
inputs: { term: str }
outputs: { full_name: str, explanation: str }
```

### Body convention

Plain markdown — purpose, when-to-use, step-by-step instructions, examples, edge cases. Body becomes the system prompt (workflow/domain inline) or the tool-call system prompt (helper invocation).

### Example — `workflow/balancing.md`

```markdown
---
name: Balancing
description: Merge ReportDraft and TeamDraft into a single reviewer-ready answer
type: workflow
owner: [orchestrator]
mode: inline
inputs:
  question: str
  report_draft: { coverage, answer, evidence_excerpts }
  team_draft: { answer, evidence, open_conflicts }
outputs:
  final_answer: str
  flags: list   # discrepancies between drafts
---

# Purpose
You are the balancing step. ...

# When coverage == "full"
Lead with the report's answer. Cross-check against team draft. ...

# When coverage == "partial"
...

# When coverage == "none"
Return team draft verbatim, prepend: "No prior reports for this case."

# Output format
Return JSON: { "final_answer": "...", "flags": [...] }
```

### Loader contract (sketch)

```python
def load_skill(path: str) -> Skill: ...
def load_skills_for(agent_name: str) -> list[Skill]: ...   # filters by owner
def render_inline_prompt(skills: list[Skill]) -> str: ...  # concatenates bodies
def helper_tool_specs(skills: list[Skill]) -> list[ToolSpec]: ...  # for LLM tool-call API
```

## §4 — LangGraph topology + LangChain LLM layer

### Stack

- **Orchestration:** LangGraph `StateGraph`. Topology, conditional edges, `Send` API for parallel fan-out, automatic gather at join nodes.
- **LLM I/O:** LangChain `ChatOpenAI` / `ChatAnthropic`. Structured output via `with_structured_output(...)`. Tool calling via `bind_tools(...)`.
- **Safety chokepoint:** `FirewallStack.wrap(model)` returns a `Runnable` that brackets `.ainvoke()` with redact pre-check, output scrubbing, shape validation, rate-limit semaphore. Same firewall wraps every inter-node transit as middleware.

### State container

```python
# models/pipeline_state.py
class PipelineState(BaseModel):
    # input
    question: str
    case_folder: Path
    pillar: str
    correlation_id: str
    # guardrail
    guardrail_verdict: GuardrailVerdict | None = None
    # parallel branches (each populated by its node)
    report_draft: ReportDraft | None = None
    team_draft: TeamDraft | None = None
    # team workflow internals
    team_plan: list[TeamAssignment] = []
    specialist_outputs: dict[str, SpecialistOutput] = {}
    review_report: ReviewReport | None = None
    # final
    final_answer: FinalAnswer | None = None
    formatted_output: str | None = None
```

### Graph topology

```
START
  └─→ guardrail_node ──reject──→ END
        └─pass─→ chat_intake_node
                   └─→ orchestrator_router_node
                         │      (Send to both branches in parallel)
                         ├──→ report_agent_node ─────────────────┐
                         │      (needle → analysis sequentially  │
                         │       inside the node)                │
                         │                                       │
                         └──→ team_construction_node             │
                                 └─→ Send(specialist_node)×N     │
                                       └─→ general_specialist_node
                                             └─→ team_synthesis_node
                                                          ▼
                                                      (gather)
                                                          ▼
                                                  balancing_node
                                                          ▼
                                                  chat_format_node
                                                          ▼
                                                         END
```

### LLM construction

```python
# gateway/llm_factory.py
def build_llm(model_name: str, firewall: FirewallStack) -> Runnable:
    base = ChatOpenAI(model=model_name)              # or ChatAnthropic, etc.
    return firewall.wrap(base)                        # adds pre/post callbacks
```

`FirewallStack.wrap(model)`:

- pre: redact identifiers in input messages, optional structured-output schema injection
- post: scrub output, validate shape, log event
- semaphore for rate limiting (configurable cap)

### Helper skills as LangChain tools

```python
@tool
def acropedia_lookup(term: str) -> dict:
    """<body of helper/acropedia.md>"""    # docstring = skill body
    return acropedia_client.lookup(term)

# Chat agent / Base specialist binds them:
self.llm = firewall.wrap(ChatOpenAI(...).bind_tools([acropedia_lookup, web_browser]))
```

### Inter-agent transit middleware

```python
def with_firewall_transit(node_fn, edge_meta):
    async def wrapped(state: PipelineState) -> dict:
        await firewall.on_node_enter(node_fn.__name__, state)
        delta = await node_fn(state)
        await firewall.on_node_exit(node_fn.__name__, delta, edge_meta)
        return delta
    return wrapped

graph.add_node("balancing", with_firewall_transit(balancing_node, {...}))
```

Every node is wrapped. Each transition logs + applies redact + shape-validates the partial state delta.

## §5 — Component spec (new agents + skill + types)

### New types in `models/types.py`

```python
class ReportDraft(BaseModel):
    coverage: Literal["full", "partial", "none"]
    answer: str
    evidence_excerpts: list[str] = []
    files_consulted: list[str] = []

class TeamDraft(BaseModel):
    # Today's FinalOutput becomes TeamDraft (the team-side intermediate)
    answer: str
    specialists_consulted: list[str]
    sub_questions: list[TeamAssignment]
    # …existing fields (open_conflicts, data_gaps, etc.) carry over

class FinalAnswer(BaseModel):
    answer: str
    flags: list[str] = []      # discrepancies surfaced by Balancing
    report_draft: ReportDraft
    team_draft: TeamDraft

class GuardrailVerdict(BaseModel):
    passed: bool
    reason: str = ""
    redacted_question: str
```

### `agents/report_agent.py`

```python
class ReportAgent:
    def __init__(self, llm, logger, skills_loader): ...
    async def run(self, question: str, case_folder: Path) -> ReportDraft:
        # 1. inline-load report_needle.md → LLM call → FileSelection
        # 2. read selected files via plain fs (no LLM)
        # 3. inline-load report_analysis.md → LLM call → ReportDraft
```

Inside the LangGraph topology this becomes `report_agent_node(state) -> {"report_draft": ReportDraft(...)}`.

### `agents/guardrail_agent.py`

```python
class GuardrailAgent:
    def __init__(self, llm, logger, skills_loader): ...
    async def screen(self, question: str) -> GuardrailVerdict:
        # 1. inline-load redact.md → scrub identifiers in question
        # 2. inline-load relevance_check.md → in-scope? if not, return reject
```

### `agents/data_manager_agent.py`

```python
class DataManagerAgent:
    def __init__(self, gateway, catalog, llm, logger, skills_loader): ...
    async def query(self, table: str, filters: dict) -> QueryResult:
        # inline-load redact.md + data_catalog.md
        # actual query_table call stays sync (in-memory data)
    async def describe_catalog(self) -> CatalogView:
        # serves the orchestrator's data_catalog skill consumers
```

### `workflow/balancing.md` invocation

```python
# Orchestrator invocation (inside balancing_node):
final = await orchestrator.balance(
    question=q,
    report_draft=report_draft,    # may have coverage="none"
    team_draft=team_draft,
)
# Internally: render_inline_prompt([balancing_skill]) + llm.ainvoke → FinalAnswer
```

### Wire-up

```python
async def run(self, question: str, case_folder: Path) -> FinalAnswer:
    report_draft, team_draft = await asyncio.gather(
        self.report_agent.run(question, case_folder),
        self._run_team_workflow(question),
    )
    return await self.balance(question, report_draft, team_draft)
```

In LangGraph this `asyncio.gather` is implicit — both `report_agent_node` and `team_construction_node` are dispatched via `Send` from `orchestrator_router_node`; the framework gathers at `balancing_node`.

## §6 — Migration sequence

Each phase lands independently, system stays runnable, dedicated tests. Roughly one PR per phase.

| # | Phase | What lands | Done when |
|---|---|---|---|
| **0a** | LangChain LLM shim | Add `langchain-openai`, `langgraph` deps. New `gateway/llm_factory.py` returns `firewall.wrap(ChatOpenAI(...))`. Existing imperative agents updated to call `await self.llm.ainvoke(...)` instead of `self.firewall.call(...)`. | Behavior preserved, no graph yet, all current tests pass |
| **0b** | Async sweep | Convert all agent methods + tests to async. Smaller than originally planned (LLM I/O already async via 0a). | `pytest -W error` clean across the suite |
| **0c** | First StateGraph | Define `PipelineState`. Build a sequential `StateGraph` mirroring today's flow (no parallelism yet, no Reports path). Behind a `--use-graph` flag. | Snapshot tests prove parity between imperative and graph paths |
| **1** | Skill loader scaffolding | `skills/loader.py` (`Skill` model, frontmatter parser, `load_skills_for`, `render_inline_prompt`, `helper_tool_specs`). `skills/{workflow,domain,helper}/` dirs created empty. | Loader can read a sample skill file and reject malformed frontmatter |
| **2** | Migrate prompts → skills (behavior-preserving) | Move `SELECT_TEAM_PROMPT` + `SPLIT_SUBQUESTIONS_PROMPT` → `workflow/team_construction.md`, `SYNTHESIZE_PROMPT` → `workflow/synthesis.md`, `COMPARE_SYSTEM_PROMPT` → `workflow/comparison.md`, `BASE_INSTRUCTIONS` → `workflow/data_query.md` + `workflow/data_analysis.md`. Convert `skills/domain/*.py` → `skills/domain/*.md` (preserve all `data_hints`/`risk_signals` in frontmatter). Delete the Python `DomainSkill` factories. | Same questions return semantically equivalent answers (snapshot tests on a fixed seed) |
| **3** | New types + Report Agent node | Add `ReportDraft`/`TeamDraft`/`FinalAnswer`. Build `report_agent_node` (single node, internally chains needle → analysis sequentially). Stage `results/CASE-00001/`. | `report_agent_node` returns a valid ReportDraft for the staged case (full / partial / none cases all tested) |
| **4** | Parallel orchestrator + Balancing node | Wire `report_agent_node` and `team_construction_node` as parallel branches via `Send`. Specialist fan-out via `Send(specialist_node)`. Pairwise comparison fan-out same pattern. New `balancing_node`. Drop `--mode report` from CLI. Cut over from `--use-graph` flag to default. | End-to-end Q on staged case routes through both paths, balancing produces a coherent merged answer |
| **5** | Guardrail node | `guardrail_node` with conditional edge to `END` on reject. Wired upstream of `chat_intake_node`. | "What should I eat for lunch?" returns reject; "Bureau status?" passes through |
| **6** | Data Manager node + data_catalog skill | Build `DataManagerAgent` owning `redact.md` (shared) + `data_catalog.md` (shared). Move catalog access from inline orchestrator code into the skill. Orchestrator inline-injects same `data_catalog.md` for team_construction + synthesis. | Catalog responses route through Data Manager; orchestrator team selection still passes the §2 regression tests |
| **7** | Firewall middleware everywhere | `with_firewall_transit` wrapper applied to every node via `graph.add_node`. Per-edge redact + log + shape validation. Semaphore configured in `firewall.wrap`. | Per-question event log shows every cross-agent edge; redaction fires on at least one edge per case |
| **8** | Helpers as tools | `acropedia_lookup`, `web_browser` as LangChain `@tool` functions; bound via `model.bind_tools(...)` on Chat / Guardrail / Base Specialist node LLMs. | Chat agent can call acropedia for "what does DTI mean?" |
| **9** | Cleanup | Remove imperative `Orchestrator.plan_team` / `synthesize` / `compare` (now in nodes). Drop `--use-graph` flag. Refresh `architecture-current.md`. | No references to `SELECT_TEAM_PROMPT` / `BASE_INSTRUCTIONS` / `mode == "report"` remain |

**Sizing:** Phases 0a–0c, 4 are the largest. Phases 2, 7 are the most invasive (touch many files). Phases 5, 6, 8 are small. Phase 9 is sweep-up.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `--mode report` removal breaks downstream callers expecting full report generation | Keep the team-only path callable as `_run_team_workflow`; expose a separate `tools/regenerate_report.py` script if anyone still needs offline regeneration |
| LangGraph parallel fan-out trips OpenAI rate limits | `firewall.wrap` includes a configurable `asyncio.Semaphore`; default low, tune via env var |
| Markdown skill bodies drift from the shape `Orchestrator` expects (missing JSON keys etc.) | Loader validates frontmatter; node functions use LangChain `with_structured_output(SchemaModel)` so output shape is enforced at the LLM boundary |
| `SessionRegistry` warmth races under parallel specialist dispatch | Convert `get_or_create` to `async` with an `asyncio.Lock`, or eagerly construct all needed specialists in the router node before the `Send` fan-out |
| Mixed sync/async during the transition | Phase 0b explicitly converts everything; `pytest -W error` catches unawaited coroutines; type-check with `mypy --strict` on firewall + agent layers |
| Reports path returns nothing useful if `results/<case-id>/` is empty | Needle returns `coverage="none"`; balancing skill body explicitly handles this case (return team draft verbatim with prefix note) |

## Open questions

None at spec time. All decisions captured above were confirmed during brainstorming on 2026-04-23.

## Success criteria

- A reviewer's question on a case with staged reports gets answered by the merged Reports + Team flow, with the Balancing skill's policy visibly applied (verifiable by editing `workflow/balancing.md` and observing different merge behavior).
- A reviewer's question on a case with no staged reports gets answered by the Team workflow alone, with a one-line "no prior reports" prefix.
- An off-topic question ("what's for lunch?") is rejected by the Guardrail Agent before any orchestrator work begins.
- All inline prompts present in today's `orchestrator/orchestrator.py`, `agents/general_specialist.py`, `agents/base_agent.py`, and `skills/domain/*.py` are gone — every prompt lives in a markdown skill file.
- `pytest -W error` passes; existing snapshot tests prove behavior parity at each phase boundary.
- The case folder + Lumi distinction from `architecture-v8.html` is reflected in code: case-folder is the default substrate, Data Manager fronts the gateway, Lumi pull is a TODO clearly marked at the gateway boundary.
