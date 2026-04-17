# Agentic Case Review v7 — POC Design Spec

**Date:** 2026-04-17
**Status:** Draft
**Architecture reference:** `architecture-v7.html`
**Prior system reference:** `AgenticSys` (v0) — LLM gateway patterns, SafeChain integration

---

## 1. Goal

Build a working POC of the v7 architecture: a multi-pillar agentic case review system where one Base Specialist Agent, configured via domain skills and pillar YAML, replaces separate specialist agent classes. A General Specialist with a Compare skill detects and resolves cross-domain contradictions. The system runs on GPT-4.1 via SafeChain in the deployment environment.

### POC Scope

- Single-process monolith (no distributed infra)
- One case at a time
- Simulated data (YAML-driven, contributor-friendly)
- Developer-facing JSON logs (no UI interpretation layer yet)
- CLI entry point + thin Flask API for future UI hookup

### Out of Scope

- Multi-user / concurrent sessions
- Real data integration
- Log visualization / audit UI
- Production deployment pipeline

---

## 2. Project Structure

```
AgenticSys_v1/
├── config/
│   ├── pillars/                # credit_risk.yaml, escalation.yaml, cbo.yaml
│   └── data_profiles/          # bureau_full.yaml, txn_monthly.yaml, ...
├── gateway/
│   ├── llm_adapter.py          # BaseLLMAdapter ABC
│   ├── openai_adapter.py       # OpenAI adapter (local dev, GPT-4.1)
│   ├── safechain_adapter.py    # SafeChain adapter (deployment)
│   └── firewall_stack.py       # Retry stack with rollback for firewall rejections
├── data/
│   ├── gateway.py              # DataGateway ABC + SimulatedDataGateway
│   ├── generator.py            # Reads data_profiles/ YAMLs → simulated tables
│   ├── access_control.py       # PillarAccess + redaction
│   └── catalog.py              # Shared data catalog (table registry, schema descriptions)
├── agents/
│   ├── base_agent.py           # BaseSpecialistAgent
│   ├── general_specialist.py   # GeneralSpecialist (Compare + Data Request)
│   └── session_registry.py     # Active specialist tracking + reuse
├── skills/
│   ├── domain/                 # bureau.py, crossbu.py, modeling.py, ...
│   │   ├── bureau.py
│   │   ├── crossbu.py
│   │   ├── modeling.py
│   │   ├── spend_payments.py
│   │   ├── wcc.py
│   │   ├── customer_rel.py
│   │   └── capacity_afford.py
│   ├── shared.py               # DataRequest, Synthesize, Report, Answer
│   └── compare.py              # Compare skill (General Specialist only)
├── orchestrator/
│   ├── orchestrator.py         # Orchestrator agent — Report Check, Team Construction, Synthesize
│   ├── chat_agent.py           # Chat agent — Converse, Stream
│   └── team.py                 # Team construction logic + specialist dispatch
├── logging/
│   └── event_logger.py         # Structured JSON event logger
├── tools/
│   └── data_tools.py           # @tool functions: query_table, get_schema, list_tables
├── main.py                     # CLI entry point
├── server.py                   # Flask API (thin, for future UI)
└── requirements.txt
```

---

## 3. Core Abstractions

### 3.1 Base Specialist Agent + Domain Skill

One agent class becomes any domain specialist via injection of a `DomainSkill` and pillar YAML config.

```python
class DomainSkill:
    name: str                    # "bureau", "crossbu", "modeling", ...
    system_prompt: str           # Domain expertise injected into every LLM call
    data_hints: list[str]        # Suggested tables/variables this domain cares about
    interpretation_guide: str    # How to read patterns in this domain
    risk_signals: list[str]      # What anomalies to flag

class BaseSpecialistAgent:
    def __init__(self, domain_skill: DomainSkill, pillar_yaml: dict, adapter: BaseLLMAdapter):
        self.skill = domain_skill
        self.pillar = pillar_yaml       # focus, prompt_overlay from YAML
        self.adapter = adapter
        self.rolling_summary = ""       # Accumulates across invocations in session

    def run(self, question: str, mode: str) -> SpecialistOutput:
        """
        question: the sub-question distributed by the orchestrator
        mode: "report" or "chat"
        """
        # 1. Build system prompt = base instructions + domain_skill + pillar overlay + rolling_summary
        # 2. data_request(question) → determine what data is needed to answer THIS question
        # 3. synthesize(question, data) → findings oriented toward answering the question
        # 4. report(question, findings) or answer(question, findings) depending on mode
        # 5. Update rolling_summary with question + key findings
        # 6. Return output

    # Shared skills — all receive the question as context:
    def data_request(self, question: str) -> DataRequestResult: ...
    def synthesize(self, question: str, data: dict) -> SynthesisResult: ...
    def report(self, question: str, findings: SynthesisResult) -> ReportSection: ...
    def answer(self, question: str, findings: SynthesisResult) -> AnswerResult: ...
```

**Prompt composition** — three layers stacked at call time:

| Layer | Source | Content |
|---|---|---|
| Layer 3 (top) | Pillar YAML | `focus` + `prompt_overlay` (e.g. "Focus: Delinquency Risk", "Overlay: Flag 90D+ Marks") |
| Layer 2 | Domain Skill | System prompt, data hints, interpretation guide, risk signals |
| Layer 1 (base) | Hardcoded | Shared instructions: output format, tool usage, data request protocol, synthesis/report/answer structure |

**Rolling summary** — after each invocation, a concise summary of findings is appended. On reuse, injected as prior context. Kept under 500-token budget; oldest entries trimmed when exceeded.

**Adding a new specialist** = writing one new file in `skills/domain/`. No agent code changes.

### 3.2 Session Registry

Manages specialist lifecycle within a case review session.

```python
class SessionRegistry:
    _active: dict[tuple[str, str], BaseSpecialistAgent]  # (domain, pillar) → instance

    def get_or_create(self, domain, pillar, domain_skill, pillar_yaml, adapter):
        # Returns existing instance if warm (rolling summary intact)
        # Creates new instance if cold

    def list_active(self) -> list[dict]:
        # Returns registry for orchestrator: domain, pillar, questions_answered, summary_preview

    def clear(self):
        # End of session — release all specialists
```

- Orchestrator calls `get_or_create()` for each specialist needed
- Warm specialists carry forward rolling summary from prior questions
- `list_active()` informs team construction — prefer reusing warm specialists
- Session boundary = one case review; `clear()` on new case

### 3.3 General Specialist & Compare

Separate agent (not a Base Agent invocation). Receives all domain specialist outputs, runs Compare.

```python
class GeneralSpecialist:
    def compare(self, specialist_outputs: dict[str, SpecialistOutput],
                question: str) -> ReviewReport:
        # 1. Extract implications from each specialist output
        # 2. Generate all relevant pairs
        # 3. For each pair: detect contradictions
        # 4. For each contradiction:
        #    a. Raise question
        #    b. Self-answer from combined evidence
        #    c. If insufficient → data_request() for more data
        #    d. If still unresolvable → flag as open conflict
        # 5. Return ReviewReport

class ReviewReport:
    resolved: list[Resolution]          # contradiction + question + answer + reasoning
    open_conflicts: list[Conflict]      # unresolvable — needs human judgment
    cross_domain_insights: list[str]    # emergent findings
    data_requests_made: list[dict]      # additional data fetched during resolution

class Resolution:
    pair: tuple[str, str]               # e.g. ("bureau", "spend_payments")
    contradiction: str
    question_raised: str
    answer: str
    supporting_evidence: list[str]
    conclusion: str                     # which implication holds and why
```

### 3.4 Orchestrator Synthesize

Merges three input streams into the final output:

1. **Specialist outputs** — domain findings (minus contradiction parts, superseded by resolutions)
2. **Review report** — resolved contradictions woven into the narrative, cross-domain insights
3. **Open conflicts** — flagged for human judgment, never buried

**Special handling:**

- **Absence-as-signal:** When a specialist reports missing data, the synthesizer evaluates whether the absence is meaningful (e.g. no bureau record → possible thin-file risk). Surfaces interpretation explicitly.
- **Blocked steps:** Firewall-blocked analyses surfaced as "analysis incomplete" with reason, never silently omitted.

```python
class FinalOutput:
    answer: str                             # coherent synthesized answer/report
    resolved_contradictions: list[Resolution]
    open_conflicts: list[Conflict]          # explicit flag for reviewer
    data_gaps: list[DataGap]                # missing data + absence interpretation
    blocked_steps: list[BlockedStep]        # firewall-blocked analyses
    specialists_consulted: list[str]

class DataGap:
    specialist: str
    missing_data: str
    absence_interpretation: str             # what the absence might mean
    is_signal: bool                         # synthesizer's judgment
```

---

## 4. LLM Gateway

### 4.1 Adapter Interface

```python
class BaseLLMAdapter(ABC):
    @abstractmethod
    def run(self, system_prompt: str, user_message: str,
            tools: list, output_type: type[BaseModel],
            max_turns: int = 12) -> BaseModel: ...

    @abstractmethod
    def chat_turn(self, messages: list[dict]) -> str: ...
```

### 4.2 OpenAI Adapter (Local Dev)

- Direct OpenAI API, model: `gpt-4.1`
- Native function calling, structured outputs via `response_format`
- Requires `OPENAI_API_KEY`

### 4.3 SafeChain Adapter (Deployment)

- Receives a SafeChain LLM object, model: `gpt-4.1`
- Manual tool-calling loop (no native function calling):
  1. Inject tool schemas into system prompt
  2. LLM outputs `{"tool_call": {"name": "...", "args": {...}}}`
  3. Dispatch tool, append result to messages
  4. Repeat until `{"output": {...}}`
- Role labels neutralized for firewall: `system → "Context:"`, `user → "Request:"`, `assistant → "Response:"`
- All messages combined into single human message via `ValidChatPromptTemplate`
- On 401 (token expiry): refresh LLM via `safechain_model()`, retry once

### 4.4 Firewall-Aware Patterns

| Threat | Mitigation |
|---|---|
| Role injection (`[SYSTEM]`, `[USER]`) | Neutral labels: "Context:", "Request:", "Response:" |
| Raw PII / account numbers | Pre-sanitize via redaction before sending |
| Code execution keywords (`exec`, `eval`, `import`) | Strip from tool results before appending |
| Large tool results (>4K tokens) | Truncate with `[truncated]` marker |
| Output-side rejection | Handled by FirewallStack (see below) |

---

## 5. Firewall Retry Stack

Wraps every LLM call. Handles content-level firewall rejections at the agent step level.

```python
class FirewallStack:
    step_history: list[StepRecord]      # stack of completed steps

    def call(self, system_prompt, user_message, tools, output_type) -> LLMResult:
        # On success: record step, return result
        # On FirewallRejection (4xx):
        #   1. Log rejection event
        #   2. Add firewall guidance to prompt ("avoid raw numbers, use masked IDs...")
        #   3. Sanitize message
        #   4. Retry (up to max_retries, default 2)
        #   5. If exhausted: return LLMResult(status="blocked", error=...)

    def rollback_to(self, step_index: int):
        # Pop steps back to checkpoint — used when multi-step chain fails partway
```

**Integration layers:**

```
Agent skill method
  → FirewallStack.call()         ← content-level retry/rollback
      → SafeChainAdapter.run()   ← protocol-level firewall handling
          → _invoke_safechain()  ← role neutralization, token refresh
```

---

## 6. Event Logger

Append-only JSON-line logger. One file per session (`logs/{session_id}.jsonl`).

### Event Types

| Event | When |
|---|---|
| `session_start` / `session_end` | Case opened / closed |
| `orchestrator_dispatch` | Question decomposed, specialists selected |
| `specialist_invoked` / `specialist_reused` | Base agent created or returned from registry |
| `data_request` / `data_response` | Specialist asks for data / data returned |
| `synthesis` | Specialist synthesizes findings |
| `report_generated` / `answer_generated` | Output produced |
| `compare_start` | General specialist begins pairwise review |
| `contradiction_found` | Pair identified, contradiction described |
| `question_raised` | General specialist formulates question |
| `self_answer` | Resolution or open conflict |
| `additional_data_request` | General specialist requests more data during compare |
| `orchestrator_synthesize` | Final synthesis begins |
| `data_gap_flagged` | Absence-as-signal evaluation |
| `firewall_rejection` / `firewall_retry` / `firewall_blocked` | Firewall events |
| `final_output` | Answer delivered to chat agent |

### Event Schema

```json
{
  "timestamp": "ISO-8601",
  "session_id": "string",
  "trace_id": "string",
  "event": "event_type",
  "...payload fields"
}
```

All events within one question flow share the same `trace_id` for reconstruction.

---

## 7. Simulated Data

### 7.1 YAML Data Profiles

One file per table in `config/data_profiles/`. Each defines:

- **Column definitions:** name, type, range, distribution (normal/poisson/uniform/categorical), distribution parameters, human-readable description
- **Correlations:** cross-column relationships (direction + strength)
- **Table metadata:** grain, default row count, description

Example (`bureau_full.yaml`):

```yaml
table: bureau_full
description: "Full bureau credit file — one row per case"
grain: one_row_per_case
row_count: 50

columns:
  case_id:
    type: string
    format: "CASE-{seq:05d}"
  score:
    type: int
    range: [300, 850]
    distribution: normal
    mean: 680
    std: 75
    description: "Credit score — FICO-like"
  derog_count:
    type: int
    range: [0, 12]
    distribution: poisson
    lambda: 1.2
    description: "Number of derogatory marks"

correlations:
  - columns: [score, derog_count]
    direction: negative
    strength: 0.7
```

### 7.2 Generator

Reads all YAML profiles, produces in-memory tables respecting distributions and correlations. Optionally dumps to CSV.

```bash
python -m data.generator --output data/simulated/ --seed 42
python -m data.generator --output data/simulated/ --seed 42 --row-count 200
```

### 7.3 Contribution Workflow

Domain experts refine simulated data by editing YAML profiles:

| Tunable | What to adjust | Example |
|---|---|---|
| `range` | Min/max realistic values | Score: [300, 850] |
| `distribution` / params | Shape of data | normal(mean=680, std=75) |
| `categories` + weights | Categorical column values | status: on_time(0.7), late(0.2), missed(0.1) |
| `correlations` | Cross-column relationships | score ↔ derog_count: negative, 0.7 |
| `description` | What the column means | Self-documenting for new contributors |

No code changes needed. Edit YAML, re-run generator.

---

## 8. Deployment Constraints

| Constraint | Handling |
|---|---|
| **Model:** GPT-4.1 only | Both adapters default to `gpt-4.1` |
| **LLM gateway:** SafeChain | `SafeChainAdapter` with role neutralization, `ValidChatPromptTemplate`, token refresh |
| **Bidirectional firewall** | Input: sanitize before sending. Output: `FirewallStack` retry with rollback. |
| **No native function calling** | Manual tool-calling loop in SafeChain adapter |

---

## 9. Key Design Decisions

| Decision | Rationale |
|---|---|
| Clean rewrite, reference v0 as guide | v1 architecture is fundamentally different; avoid carrying v0 dead code. Same abstractions, fresh implementation. |
| Monolith-first | POC goal is validating the architecture, not scaling. Single process, in-memory state. |
| Rolling summary (not full history) for reuse | Balances continuity with context window limits on GPT-4.1. |
| Developer JSON logs only | Interpretation/viz layer is next stage. Keep POC focused. |
| YAML data profiles | Explicit, version-controllable, self-documenting. Domain experts edit YAML, not code. |
| General Specialist as separate agent | Not a Base Agent invocation — it has a fundamentally different role (cross-domain review vs. domain analysis). |
| Firewall Stack above adapter | Adapter handles protocol-level issues; Stack handles content-level retry/rollback. Clean separation. |
