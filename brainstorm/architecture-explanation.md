---
title: "AgenticSys v2 — Architecture Explanation"
subtitle: "Wiring, Skills, and Tools under the OpenAI Agents SDK"
author: "AgenticSys v2 — post openai-agents migration"
date: "2026-04-28"
geometry: margin=1in
---

# Overview

This document explains how the AgenticSys v2 orchestrator coordinates with other agents,
how agents consume "skills," and how tools are dispatched at runtime. It reflects the
architecture after the migration to the OpenAI Agents SDK
(plan: `docs/plans/2026-04-28-openai-agents-sdk-migration.md`,
spec: `docs/specs/2026-04-28-openai-agents-sdk-migration-design.md`).

# 1. How the orchestrator is wired with other agents

The wiring happens **once** in `Orchestrator.__init__` (`orchestrator/orchestrator.py`).
When the caller hands in a `clients=SessionClients(...)` (from `build_session_clients`),
the constructor builds a graph of five factory-produced Agents, all sharing one
firewalled `AsyncOpenAI` client.

```
                    AsyncOpenAI (real)
                          |
                          v
                FirewalledAsyncOpenAI         <- PII redaction +
                          |                      retry-with-guidance +
                          v                      shared semaphore
            OpenAIChatCompletionsModel
                          |
        +-----------------+----------------+--------------+-------------+
        |                 |                |              |             |
   orchestrator    [7 specialist]    report_agent   general_      (every Agent
     (Agent)          Agents          (Agent)       specialist    uses the SAME
                                                     (Agent)      shared model)
```

## Construction sequence

The `__init__` block when `clients is not None`
(`orchestrator/orchestrator.py`):

```python
domain_names = list_domain_skills()  # 7 names from skills/domain/
specialists = [
    build_specialist_agent(load_domain_skill(d), self.pillar_config, model=clients.model)
    for d in domain_names
]
self.report_agent_obj = build_report_agent(model=clients.model)
self.general_agent    = build_general_specialist(model=clients.model)
self.orchestrator_agent = build_orchestrator_agent(
    specialists=specialists,
    report_agent=self.report_agent_obj,
    general_specialist=self.general_agent,
    model=clients.model,
)
```

The orchestrator agent is the only one that *knows about* the others — it owns them as
**tools**, not as references. From `case_agents/orchestrator_agent.py`:

```python
def build_orchestrator_agent(specialists, report_agent, general_specialist, model):
    tools = [
        redacting_tool(s, name=s.name, description=_describe_specialist(s))
        for s in specialists
    ]
    tools.append(redacting_tool(report_agent,    name="report_agent",
                                description="..."))
    tools.append(redacting_tool(general_specialist, name="general_specialist",
                                description="..."))
    return Agent(
        name="orchestrator",
        instructions=_compose_orchestrator_instructions(),
        tools=tools,
        output_type=AgentOutputSchema(FinalAnswer, strict_json_schema=False),
        model=model,
    )
```

So the orchestrator does not *call* the other agents directly. It has nine tools
(7 specialists + 1 report + 1 general), and the **LLM decides at runtime** which tools
to invoke. When the LLM emits tool calls, the SDK runs them. Parallel fan-out comes for
free: if the LLM emits multiple tool calls in one turn, OpenAI's API + the SDK execute
them concurrently.

`redacting_tool` (`case_agents/redacting_tool.py`) is the bridge — it wraps
`agent.as_tool()` so input gets `sanitize_message`-d before the inner Agent sees it,
and output gets `redact_payload`-d before going back to the orchestrator's LLM context.

## End-to-end run

The whole pipeline runs through one `Runner.run` per question
(`orchestrator/orchestrator.py`):

```python
async def run(self, question, case_folder, report_agent=None) -> FinalAnswer:
    ctx = AppContext(gateway=self.gateway, case_folder=case_folder, logger=self.logger)
    try:
        result = await Runner.run(self.orchestrator_agent, question, context=ctx)
        final = redact_payload(result.final_output)
    except AgentsException as exc:
        final = self._trace_extraction_fallback(exc)   # beta fallback
    return final
```

There is no Python-level `asyncio.gather` between agents anymore — the SDK handles
agent dispatch internally.

# 2. How agents "invoke" skills

In this codebase, **a "skill" is a markdown file**. There is no dispatch — the runtime
never *calls* a skill. Skills become part of an Agent's `instructions=` string at
construction time, and the LLM reads them as part of its system prompt.

## Two kinds of skills

**Workflow skills** (`skills/workflow/`) — orchestrator-level prose:

- `team_construction.md` — when/how to pick specialists
- `data_catalog.md` — what data is available
- `synthesis.md` — how to merge multi-specialist findings
- `balancing.md` — how to weave Reports + Team into a final answer
- `data_query.md` — base "use the data tools" instructions for every specialist
- `comparison.md` — instructions for the general specialist
- `report_needle.md`, `report_analysis.md` — coverage rubric + evidence extraction
  for the report agent

**Domain skills** (`skills/domain/`) — per-specialist domain knowledge. One markdown
per domain (bureau, capacity_afford, modeling, etc.). Loaded via
`skills/domain/loader.py` into a typed
`DomainSkill(name, system_prompt, data_hints, interpretation_guide, risk_signals)`.

## The loader

The loader is intentionally dumb — it is just file-read + frontmatter parse. From
`skills/loader.py`:

```python
load_skill(path) -> Skill(body="<the markdown content>")
```

Each Agent factory weaves the relevant skill bodies into its `instructions` string at
module-import time:

| Agent | What gets composed | Where |
|---|---|---|
| Specialist | `data_query.md` body + skill.system_prompt + data_hints + interpretation_guide + risk_signals + pillar overlay | `case_agents/specialist_agent.py` — `_compose_instructions()` |
| Report | `report_needle.md` body + `report_analysis.md` body + workflow framing | `case_agents/report_agent.py` — `REPORT_AGENT_INSTRUCTIONS` |
| General specialist | `comparison.md` body, verbatim | `case_agents/general_specialist.py` — `COMPARE_SYSTEM_PROMPT` |
| Orchestrator | `team_construction.md` + `data_catalog.md` + `synthesis.md` + `balancing.md` bodies + inline `TOOL-USE DISCIPLINE` + `PARALLEL EXECUTION` blocks | `case_agents/orchestrator_agent.py` — `_compose_orchestrator_instructions()` |

To see the actual rendered string the orchestrator's LLM receives, see
`brainstorm/orchestrator_instructions.md` (a snapshot generated from
`_compose_orchestrator_instructions()`).

To "invoke a skill" you simply edit the markdown — next session, the new text goes into
the prompt automatically.

# 3. How tools are called

Two distinct tool types, both expressed as `FunctionTool` instances in the SDK.

## 3a. `@function_tool`-decorated Python functions

These are plain Python functions wrapped by the SDK decorator. The decorator
(`from agents import function_tool`) introspects the function's signature + docstring
and produces a `FunctionTool` with `name`, `description`, `params_json_schema`, and
`on_invoke_tool`.

### Module-state tools (`tools/data_tools.py`)

`list_available_tables`, `get_table_schema`, `query_table`. Read shared module-level
state (`_gateway`, `_catalog`, `_logger`) which `main.py` initializes once via
`init_tools(gateway, catalog, logger)` before any `Runner.run`. Each public function is
decorated:

```python
@function_tool
def query_table(table_name: str, filter_column: str = "", ...) -> str:
    ...
```

The decorated `query_table` is **not directly callable** — the SDK's `FunctionTool`
wrapper raises `TypeError` on direct call. So the codebase has `_query_table_impl` (the
real Python function) and the decorated `query_table` (the FunctionTool). Direct
callers (`data_manager_agent`, tests) import `_query_table_impl`. SDK Agents get
`query_table` as their tool.

### RunContext-aware tools (`tools/fs_tools.py`)

`fs_list_files`, `fs_read_file`. Take `ctx: RunContextWrapper[AppContext]` as their
first arg; the SDK auto-injects it from `Runner.run(..., context=AppContext(...))` and
strips `ctx` from the JSON schema the LLM sees. So the LLM only sees the user-facing
parameters (e.g., `filename` for `fs_read_file`):

```python
@function_tool
async def fs_read_file(ctx: RunContextWrapper[AppContext], filename: str) -> str:
    folder = ctx.context.case_folder    # injected by SDK
    target = (folder / filename).resolve()
    ...
```

## 3b. Agent-as-tool (`agent.as_tool()`) wrapped by `redacting_tool`

Each sub-agent is exposed as a tool to the orchestrator via
`case_agents/redacting_tool.py`:

```python
def redacting_tool(agent: Agent, name: str, description: str):
    @function_tool(name_override=name, description_override=description)
    async def _runner(ctx: RunContextWrapper, sub_question: str) -> str:
        redacted_in = sanitize_message(sub_question)              # input boundary
        result = await Runner.run(agent, redacted_in, context=ctx.context)
        return redact_payload(result.final_output)                # output boundary
    return _runner
```

This is itself a `@function_tool`, so the orchestrator's tool list is uniform: nine
`FunctionTool` instances. The LLM emits a tool call like
`creditrisk(sub_question="...")`; the SDK invokes `_runner`; `_runner` calls
`Runner.run` on the inner specialist agent (which has its own tool loop with the data
tools); the specialist returns a `SpecialistOutput`; we redact and return.

## Runtime path of a single tool call

Tracing one specialist invocation end-to-end:

1. **Orchestrator's LLM** decides to call `creditrisk(sub_question="What is the FICO trajectory?")`.
2. SDK's runner finds the matching `FunctionTool` (the `redacting_tool`-wrapped one) and calls `_runner.on_invoke_tool(ctx, json_args)`.
3. `_runner` deserializes args, calls `sanitize_message` on the sub_question (input boundary).
4. `_runner` calls `await Runner.run(creditrisk_agent, redacted_in, context=ctx.context)` — a **nested** `Runner.run`.
5. The nested run: `creditrisk_agent`'s LLM sees the question + its instructions
   (data_query.md + creditrisk skill + pillar overlay). It decides to call
   `query_table(table_name="bureau", ...)`.
6. SDK invokes `query_table.on_invoke_tool` -> which calls `_query_table_impl` ->
   which reads from the module-level `_gateway` (set by `init_tools` in `main.py`) ->
   returns a JSON string of rows.
7. The specialist's LLM continues, possibly calling more tools, and finally emits a
   `SpecialistOutput` (validated against
   `AgentOutputSchema(SpecialistOutput, strict_json_schema=False)`).
8. Back in the outer `_runner`: `result.final_output` is a `SpecialistOutput`
   Pydantic instance. `redact_payload(result.final_output)` walks it and applies
   `sanitize_message` to string fields.
9. The redacted output goes back to the orchestrator's LLM context as the tool result.
10. Orchestrator's LLM may now call other tools (in parallel or serially), then emit
    its `FinalAnswer`.

Two things underneath all of this:

- **Every** HTTP call to OpenAI in steps 1, 5, and 10 goes through
  `FirewalledAsyncOpenAI` (`llm/firewall_client.py`) — which acquires the shared
  semaphore, applies outbound redaction, and retries on `FirewallRejection` with
  `FIREWALL_GUIDANCE` injected.
- **Tool dispatch** (the SDK looking up which `FunctionTool` matches a tool_call name)
  happens inside `agents.run.Runner` — we do not write that; the SDK does.

# TL;DR mental model

- **Wiring**: a single Agent (orchestrator) holds nine tools that are wrapped
  sub-agents. One `Runner.run` per question. Parallelism = LLM emits multiple tool
  calls in one turn.
- **Skills**: markdown files concatenated into Agent `instructions` at construction
  time. The LLM "uses" them by reading them as part of its system prompt. No runtime
  dispatch.
- **Tools**: every callable surface is a `FunctionTool`. Data tools read module state
  set by `init_tools`. Filesystem tools read per-request `AppContext`. Sub-agents are
  wrapped via `redacting_tool` to enforce the inter-agent transit redaction boundary.
