# OpenAI Agents SDK Migration — Design

**Date:** 2026-04-28
**Status:** Draft, pending implementation plan
**Scope:** Replace the LangChain-backed orchestration layer with the OpenAI Agents SDK (`openai-agents`), preserving the external `Orchestrator.run` API and the FirewallStack's content-safety semantics.

## 1. Motivation

The current system uses `langchain-openai`'s `ChatOpenAI` wrapped in a custom `FirewalledModel`, with a hand-rolled orchestrator that runs the Reports + Team branches in parallel and merges via a Balancing skill. We want to replace this with the OpenAI Agents SDK — using `Agent`, `Runner`, `@function_tool`, and the agents-as-tools pattern — while keeping the production-grade firewall behaviors and the existing public surface so the test suite remains a regression net.

## 2. Decisions

Six pivotal decisions, all settled in brainstorming:

| # | Question | Decision |
|---|---|---|
| Q1 | What does "OpenAI SDK framework" mean? | **OpenAI Agents SDK** (`openai-agents`), not just the OpenAI Python client |
| Q2 | Orchestration pattern? | **Agents-as-tools** (orchestrator is an `Agent`, specialists exposed via `agent.as_tool()`) — **plus** preserve `FirewallStack` and parallel execution |
| Q3 | How much does the orchestrator agent absorb? | **A1 maximal** — one agent loop replaces `plan_team` + `_run_team_workflow` + `synthesize` + `balance` |
| Q4 | Which firewall behaviors stay faithful? | **Keep**: PII redaction, retry-with-guidance, concurrency cap. **Simplify**: inter-agent transit logging (replaced by SDK tracing). **Drop**: step-history rollback. |
| Q5 | Specialist sessions? | **Stateless** — drop `SessionRegistry` and `rolling_summary`; orchestrator's own loop provides cross-specialist context within a question |
| Q6 | Migration scope? | **Preserve external API** — `Orchestrator.run(question, case_folder, report_agent) -> FinalAnswer` signature stays; internals replaced; public `EventLogger` event names preserved (modulo verification item §7-3) |
| Q7 (β) | What if `Runner.run` exhausts retry-with-guidance? | **Trace-extraction fallback** — recover completed `ReportDraft`/`SpecialistOutput`s from `result.new_items` and stitch via the existing per-coverage-mode concatenation |

## 3. Architecture

One `Agent` graph constructed per `Orchestrator` instance, all sharing one firewalled `AsyncOpenAI` client.

### 3.1 Agent graph

| Agent | Built by | `instructions` source | `tools` | `output_type` |
|---|---|---|---|---|
| Orchestrator | `build_orchestrator_agent` | `team_construction.md` + `data_catalog.md` + `synthesis.md` + `balancing.md` (existing skill files) | redacting wrappers around: each specialist agent, `report_agent`, `general_specialist` | `FinalAnswer` |
| Specialist (one per domain) | `build_specialist_agent(skill, pillar)` | `data_query.md` + `skill.system_prompt` + pillar overlay (port of `_build_system_prompt`, minus rolling-summary) | `list_available_tables`, `get_table_schema`, `query_table` (port of `tools/data_tools.py` to `@function_tool`) | `SpecialistOutput` |
| Report | `build_report_agent` | Port of current `agents/report_agent.py` prompt | `fs_list_files`, `fs_read_file` (`@function_tool` wrappers around current FS access) | `ReportDraft` |
| General specialist | `build_general_specialist` | Port of current `GeneralSpecialist` compare prompt | none | `ReviewReport` |

### 3.2 Module layout after migration

```
agents/
  orchestrator_agent.py    # NEW: build_orchestrator_agent factory
  specialist_agent.py      # NEW: build_specialist_agent factory (replaces base_agent.py)
  report_agent.py          # MODIFIED: now build_report_agent factory
  general_specialist.py    # MODIFIED: now build_general_specialist factory
  chat_agent.py            # MOSTLY UNCHANGED (public methods preserved)
  base_agent.py            # DELETED
  session_registry.py      # DELETED
  helper_tools.py          # MODIFIED: @function_tool decorators
llm/
  firewall_client.py       # NEW: FirewalledAsyncOpenAI wrapper (§4)
  firewall_stack.py        # MODIFIED: shrunk to FirewallStack state container + sanitize/redact helpers
  factory.py               # MODIFIED: build_session_clients(firewall) -> firewalled client
  case_scrubber.py         # UNCHANGED
orchestrator/
  orchestrator.py          # MODIFIED: thin wrapper around Runner.run, with β trace-extraction fallback
```

### 3.3 Dependencies

```
- langchain-core>=0.3.0,<0.4.0       # REMOVED
- langchain-openai>=0.2.0,<0.3.0     # REMOVED
+ openai-agents                       # NEW — version pinned during implementation
  openai>=1.30.0,<2.0.0              # KEPT (already in requirements)
```

### 3.4 Dependency injection via `RunContext`

Tools are module-level `@function_tool` functions, so runtime objects (gateway, case_folder, EventLogger) flow in via the SDK's typed context:

```python
@dataclass
class AppContext:
    gateway: Any
    case_folder: Path
    logger: EventLogger

@function_tool
async def query_table(ctx: RunContextWrapper[AppContext], table: str, where: dict | None = None):
    return ctx.context.gateway.query(table, where=where)
```

`Runner.run` receives the context once at the top; the SDK auto-threads it to every tool call across the entire agent loop, including tools called inside agent-as-tool runs.

## 4. FirewallStack mapping

Three behaviors preserved faithfully (a/b/c). The injection point is the `AsyncOpenAI` HTTP client itself — every Agent built by our factories shares one wrapped client, so the behaviors apply uniformly.

### 4.1 New module: `llm/firewall_client.py`

```python
class FirewalledAsyncOpenAI:
    def __init__(self, base: AsyncOpenAI, firewall: FirewallStack):
        self._base = base
        self._firewall = firewall

    @property
    def chat(self): return _FirewalledChat(self._base.chat, self._firewall)
    # delegate everything else (.responses, .files, etc.) to self._base


class _FirewalledChatCompletions:
    async def create(self, *, model, messages, **kw):
        # (a) redact every outbound message
        messages = [_redact_message(m) for m in messages]

        attempt = 0
        while True:
            # (c) shared semaphore caps concurrent requests across all agents
            async with self._firewall.semaphore:
                try:
                    resp = await self._base.completions.create(
                        model=model, messages=messages, **kw)
                except FirewallRejection:
                    # (b) retry with guidance injected, up to max_retries
                    if attempt >= self._firewall.max_retries:
                        raise
                    attempt += 1
                    messages = _inject_guidance_and_resanitize(messages)
                    continue
            return resp
```

Wired into every Agent:

```python
client = FirewalledAsyncOpenAI(AsyncOpenAI(), firewall_stack)
model = OpenAIChatCompletionsModel(model="gpt-4o", openai_client=client)
agent = Agent(name="creditrisk", instructions=..., tools=[...], model=model)
```

### 4.2 `llm/firewall_stack.py` shrinks

Keeps only:
- `FirewallStack` — owns `semaphore`, `max_retries`, `logger`
- `FirewallRejection` — exception, unchanged
- `sanitize_message(text)` — promoted from `_sanitize_message`
- `redact_payload(payload)` — promoted from `_redact_payload`

Deleted: `FirewallStack.wrap`, `FirewalledModel`, `_tool_loop`, `step_history`, `rollback_to`, `send`.

### 4.3 Inter-agent transit redaction (residual from Q4-d)

Even with A1, three boundaries cross trust:

1. **Tool input** — orchestrator → specialist sub-question (LLM-produced, untrusted)
2. **Tool output** — specialist `SpecialistOutput` → orchestrator's LLM context
3. **Final answer** — `Orchestrator.run` → `ChatAgent`

Helper for (1)+(2):

```python
def redacting_tool(specialist_agent: Agent, name: str, description: str) -> Tool:
    raw = specialist_agent.as_tool(tool_name=name, tool_description=description)
    @function_tool(name_override=name, description_override=description)
    async def wrapped(sub_question: str) -> SpecialistOutput:
        sub_question = sanitize_message(sub_question)
        result = await raw.invoke(sub_question)  # exact SDK invocation shape verified day 1
        return redact_payload(result)
    return wrapped
```

Boundary (3) is a single `redact_payload(result.final_output)` call in `Orchestrator.run` before returning.

### 4.4 What we explicitly do NOT use

- SDK `InputGuardrail`/`OutputGuardrail` — wrong shape (abort-on-tripwire, no retry-with-guidance, no transformation)
- `set_default_openai_client(...)` — keeps the firewalled client scoped to our orchestrator instance rather than process-global

## 5. Orchestrator control flow

### 5.1 `Orchestrator.run` (preserved signature)

```python
async def run(self, question, case_folder, report_agent) -> FinalAnswer:
    ctx = AppContext(gateway=self.gateway, case_folder=case_folder, logger=self.logger)
    self.logger.log("orchestrator_run_start", {...})
    try:
        result = await Runner.run(self.orchestrator_agent, question, context=ctx)
        final = redact_payload(result.final_output)  # boundary 3
    except RunnerExhausted as e:                     # SDK exception name verified day 1
        final = self._trace_extraction_fallback(e)
    self.logger.log("orchestrator_run_done", {...})
    return final
```

### 5.2 Parallel fan-out

The OpenAI Chat Completions API natively supports parallel tool_calls (`parallel_tool_calls=true` on newer models). The orchestrator agent's instructions explicitly tell the LLM to emit multiple tool calls in one response when fanning out to specialists — the SDK schedules them concurrently. Our shared `FirewallStack.semaphore` (§4.1-c) caps real concurrency to protect against rate limits.

### 5.3 β trace-extraction fallback

When `Runner.run` raises after retry-with-guidance exhaustion:

```python
def _trace_extraction_fallback(self, exc: RunnerExhausted) -> FinalAnswer:
    # exc.run_result.new_items contains every tool-call output completed before failure
    completed = [item for item in exc.run_result.new_items
                 if isinstance(item, ToolCallOutputItem)]

    report_draft = _hydrate_first(completed, "report_agent", ReportDraft)
    specialist_outputs = _hydrate_all_matching(completed, SpecialistOutput)

    if report_draft is None and not specialist_outputs:
        return FinalAnswer(answer="Analysis was blocked by content firewall.",
                           flags=["orchestrator blocked, no partial drafts recovered"])

    # Reuse today's per-coverage concatenation logic
    return _balance_fallback_from_partials(report_draft, specialist_outputs,
                                           flag="balancing fallback: orchestrator blocked")
```

`_hydrate_first` / `_hydrate_all_matching` are small helpers that locate `ToolCallOutputItem`s by tool name and `model_validate_json` their string payload back into Pydantic shapes. The exact SDK type names are verified on day 1 (verification item §7-2).

## 6. Test strategy

Migration scope is II (preserve external API) — the test suite stays as the regression net.

### 6.1 Tests that survive unchanged

These pin externally-observable behavior, not internals:
- `tests/test_main_resolver.py` — CLI entry behavior
- `tests/test_sync.py`, `test_adapter.py`, `test_catalog_sync.py`, `test_sync_interactive_demo.py` — datalayer
- `tests/test_agents/test_chat_agent.py` — ChatAgent's public methods (screen/redact/relevance/converse) preserved
- `tests/test_agents/test_helper_tools.py` — helper tools port to `@function_tool` but signatures preserved

### 6.2 Tests deleted or rewritten

- `test_agents/test_base_agent.py` — class deleted → file deleted
- `test_agents/test_session_registry.py` — registry deleted → file deleted
- `test_agents/test_report_agent.py`, `test_general_specialist.py`, `test_data_manager_agent.py` — classes become factory functions; tests rewritten to assert factory builds an `Agent` with expected `instructions`/`tools`/`output_type`
- `test_llm/test_firewalled_model.py` — `FirewalledModel` deleted → file deleted
- `test_llm/test_firewall_stack.py` — rewritten against shrunk surface (`semaphore`, `max_retries`, `sanitize_message`, `redact_payload`)
- `test_llm/test_factory.py` — `build_llm` → new factory signature

### 6.3 New test files

- `test_llm/test_firewall_client.py` — `FirewalledAsyncOpenAI` wrapper:
  1. Outbound message redaction (mock underlying `AsyncOpenAI.chat.completions.create`, assert masking applied)
  2. Retry-with-guidance: first call raises `FirewallRejection`, assert second call has `FIREWALL_GUIDANCE` in system message
  3. Concurrency cap: launch N concurrent calls, assert at most `concurrency_cap` are in-flight (use `asyncio.Event`s in the mock)
- `test_orchestrator/test_orchestrator_run.py` — end-to-end smoke: mock `AsyncOpenAI` to script parallel tool_calls + final structured output; run `Orchestrator.run`; assert `FinalAnswer` shape + that `EventLogger` event names match
- `test_orchestrator/test_balance_fallback.py` — β: mock `AsyncOpenAI` so the orchestrator's final synthesis call exhausts retries; assert trace-extraction fallback produces `FinalAnswer` with concatenated drafts and `flags=["balancing fallback: orchestrator blocked"]`

### 6.4 Mocking strategy

Tests target the SDK's `AsyncOpenAI` boundary, not `Runner` itself. We exercise the real agent loop with a fake HTTP layer:

```python
@pytest.fixture
def mock_openai_client(scripted_responses):
    client = AsyncMock(spec=AsyncOpenAI)
    client.chat.completions.create.side_effect = scripted_responses
    return client
```

Real Runner, real tool dispatch, real parallel scheduling, deterministic responses.

## 7. Open verification items (day-1 spikes, none design-blocking)

1. **Where `FirewallRejection` is currently raised** — the exception class and retry handler are visible in `llm/firewall_stack.py`, but not the producer. Likely in `case_scrubber.py` or a tool-validation step. Find it; port 1:1 into `_FirewalledChatCompletions.create`'s post-response check.
2. **SDK API for partial trace state on `Runner.run` exception** — exact attribute name (`exc.run_result.new_items`?) and item type (`ToolCallOutputItem`?). Drives the §5.3 fallback implementation.
3. **`EventLogger` event-name external consumers** — Q6-II promised event-name preservation. Some events fire from Python stages that no longer exist as separate steps under A1 (e.g., `plan_team_start`/`done` no longer corresponds to a Python-level call). Decide: fire synthetic equivalents from `Agent.hooks`, or update the contract. If no external consumer parses logs, we have flexibility.

## 8. Out of scope

- **Streaming responses.** The current system doesn't stream; the SDK supports it but adoption is a separate change.
- **SDK Sessions for cross-question memory.** Q5 chose stateless; sessions can be added later if `rolling_summary`-equivalent behavior turns out to be missed.
- **Adopting SDK tracing as the primary observability.** `EventLogger` stays primary (Q6-II); SDK tracing is additive and replaces only the inter-agent transit logging behavior (Q4-d).
- **Migrating `ChatAgent` to be an SDK `Agent`.** ChatAgent's screen/redact/relevance/converse methods are preserved as today (one short LLM call each); reshaping them into Agents is unrelated work.
- **Changing the data tool surface.** `tools/data_tools.py` keeps the same functions and signatures, just decorated with `@function_tool`. Catalog/gateway/skill loaders unchanged.

## 9. Acceptance criteria

The migration is complete when:
1. `langchain-core` and `langchain-openai` are removed from `requirements.txt`; `openai-agents` is added.
2. `BaseSpecialistAgent`, `SessionRegistry`, `FirewalledModel` classes no longer exist in the codebase.
3. `Orchestrator.run(question, case_folder, report_agent) -> FinalAnswer` signature is unchanged; existing surviving tests pass.
4. New tests (§6.3) cover the firewall client behaviors and the β fallback.
5. The three day-1 verification items (§7) have been resolved and the spec is annotated with the findings, or this design doc is amended.
