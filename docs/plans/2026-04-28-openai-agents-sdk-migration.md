# OpenAI Agents SDK Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the LangChain-backed orchestration layer with the OpenAI Agents SDK, preserving the external `Orchestrator.run` API and FirewallStack content-safety semantics.

**Architecture:** Agents-as-tools (A1 maximal) — one orchestrator `Agent` exposes specialists + report + general as tools via `agent.as_tool()`; one `Runner.run` per question; parallel fan-out comes from the OpenAI API's native parallel tool_calls. PII redaction, retry-with-guidance, and concurrency cap are preserved by wrapping `AsyncOpenAI` in a `FirewalledAsyncOpenAI` that every Agent shares.

**Tech Stack:** `openai-agents` (PyPI) imports as `agents` (Python), `openai>=1.30`, Pydantic v2, pytest-asyncio.

**Spec:** [docs/specs/2026-04-28-openai-agents-sdk-migration-design.md](../specs/2026-04-28-openai-agents-sdk-migration-design.md)

**Implementation deviation from spec:** The spec describes `RunContext` for tool dependency injection (§3.4). Today's `tools/data_tools.py` uses module-level state via `init_tools(gateway, catalog, logger)`. To minimize blast radius, we **keep the module-level state pattern** for the existing tools — just add `@function_tool` decorators. Only the *new* `fs_*` tools for the report agent take a context arg, since `case_folder` is genuinely per-request.

---

## Phase 0 — Namespace, dependency, spike

### Task 0.1: Rename local `agents/` to `case_agents/`

The OpenAI Agents SDK installs as `openai-agents` but imports as `from agents import ...`. The local `agents/` directory collides with this. Rename before installing the SDK.

**Files:**
- Rename: `agents/` → `case_agents/`
- Rename: `tests/test_agents/` → `tests/test_case_agents/`
- Modify: every `.py` file containing `from agents.` or `import agents.`

- [ ] **Step 1: Find all import sites**

```bash
cd /path/to/AgenticSys_v2
grep -rln "from agents\.\|import agents\." --include="*.py" . > /tmp/agents_imports.txt
cat /tmp/agents_imports.txt
```

Expected: a list of files including `main.py`, `orchestrator/orchestrator.py`, agent files themselves, and tests.

- [ ] **Step 2: Rename directories**

```bash
git mv agents case_agents
git mv tests/test_agents tests/test_case_agents
```

- [ ] **Step 3: Update all imports across the codebase**

```bash
# macOS sed; on Linux drop the '' after -i
find . -type f -name "*.py" \
  -exec sed -i '' 's/from agents\./from case_agents\./g; s/import agents\./import case_agents\./g' {} +
```

Verify nothing was missed:
```bash
grep -rn "from agents\.\|import agents\." --include="*.py" .
```
Expected: no results.

- [ ] **Step 4: Run the existing test suite to verify rename is clean**

```bash
pytest -q
```
Expected: all tests pass (same count as before).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: rename local agents/ to case_agents/ for SDK namespace"
```

---

### Task 0.2: Add openai-agents dependency and verify it imports

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add `openai-agents` to requirements.txt**

Insert (keep existing pins; add this line near the other openai-related lines):
```
openai-agents>=0.0.10
```

- [ ] **Step 2: Install**

```bash
pip install -r requirements.txt
```
Expected: `openai-agents` resolves and installs without conflict.

- [ ] **Step 3: Smoke-test the import**

```bash
python -c "from agents import Agent, Runner, function_tool, RunContextWrapper; print('SDK import OK')"
```
Expected: `SDK import OK` (and no `ImportError`).

- [ ] **Step 4: Verify the SDK's `agents` does not collide with `case_agents/`**

```bash
python -c "from case_agents.chat_agent import ChatAgent; from agents import Agent; print('Both import paths work')"
```
Expected: `Both import paths work`.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "feat: add openai-agents dependency"
```

---

### Task 0.3: Spike — verify SDK partial-trace API for β fallback

The β trace-extraction fallback (spec §5.3) needs to access completed `ToolCallOutputItem`s after `Runner.run` raises. Verify the exact attribute names by writing a tiny script.

**Files:**
- Create: `scripts/spike_runner_trace.py` (throwaway, deleted at end of task)

- [ ] **Step 1: Write the spike script**

Create `scripts/spike_runner_trace.py`:
```python
"""Spike: inspect what's available on RunResult and on exceptions raised by Runner.run."""
import asyncio
from agents import Agent, Runner, function_tool

@function_tool
def echo(text: str) -> str:
    return f"echoed: {text}"

@function_tool
def boom() -> str:
    raise RuntimeError("forced failure for spike")

agent = Agent(name="spike", instructions="Call echo with 'hi', then call boom.", tools=[echo, boom])

async def main():
    try:
        result = await Runner.run(agent, "go")
        print("RESULT TYPE:", type(result).__name__)
        print("RESULT ATTRS:", [a for a in dir(result) if not a.startswith("_")])
        print("new_items types:", [type(i).__name__ for i in getattr(result, "new_items", [])])
    except Exception as e:
        print("EXC TYPE:", type(e).__name__)
        print("EXC ATTRS:", [a for a in dir(e) if not a.startswith("_")])
        # Probe for partial run state
        for candidate in ("run_result", "result", "partial_result"):
            if hasattr(e, candidate):
                rr = getattr(e, candidate)
                print(f"{candidate} TYPE:", type(rr).__name__)
                print(f"{candidate}.new_items types:",
                      [type(i).__name__ for i in getattr(rr, "new_items", [])])

asyncio.run(main())
```

- [ ] **Step 2: Run the spike (requires OPENAI_API_KEY)**

```bash
OPENAI_API_KEY=$OPENAI_API_KEY python scripts/spike_runner_trace.py
```
Capture the output. Note the exception class name (e.g., `MaxTurnsExceeded`, `AgentsException`, etc.) and how to access partial trace state.

- [ ] **Step 3: Document findings inline in the spec**

Open `docs/specs/2026-04-28-openai-agents-sdk-migration-design.md` and append a "Findings" subsection to §7-2:

```markdown
**§7-2 findings (resolved YYYY-MM-DD):**
- Exception type for retry exhaustion: `<ActualClassName>`
- Partial trace access: `exc.<actual_attr>.new_items`
- ToolCallOutputItem class: `<actual_class_name>`, payload accessed via `.<attr>` (string JSON)
```

- [ ] **Step 4: Delete the spike script**

```bash
git rm scripts/spike_runner_trace.py
rmdir scripts 2>/dev/null || true
```

- [ ] **Step 5: Commit**

```bash
git add docs/specs/2026-04-28-openai-agents-sdk-migration-design.md
git commit -m "docs: resolve SDK trace API spike for orchestrator fallback"
```

---

## Phase 1 — Firewall foundation (additive, doesn't break old code)

### Task 1.1: Promote `sanitize_message` and `redact_payload` to public helpers

Today these are private methods on `FirewallStack`. Promote them to module-level functions so the new client wrapper and tool wrappers can import them without holding a `FirewallStack` instance.

**Files:**
- Modify: `llm/firewall_stack.py`
- Test: `tests/test_llm/test_firewall_helpers.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/test_llm/test_firewall_helpers.py`:
```python
from llm.firewall_stack import sanitize_message, redact_payload
from pydantic import BaseModel

def test_sanitize_message_masks_case_id():
    assert sanitize_message("CASE-12345 review") == "[CASE-ID] review"

def test_sanitize_message_masks_long_digits():
    assert sanitize_message("acct 1234567890 details") == "acct ***MASKED*** details"

def test_redact_payload_walks_nested_dict():
    payload = {"meta": {"case": "CASE-9999"}, "items": ["acct 1234567"]}
    out = redact_payload(payload)
    assert out["meta"]["case"] == "[CASE-ID]"
    assert out["items"][0] == "acct ***MASKED***"

def test_redact_payload_pydantic_roundtrip():
    class M(BaseModel):
        note: str
    out = redact_payload(M(note="CASE-42"))
    assert isinstance(out, M)
    assert out.note == "[CASE-ID]"
```

- [ ] **Step 2: Run tests, expect failure**

```bash
pytest tests/test_llm/test_firewall_helpers.py -v
```
Expected: `ImportError: cannot import name 'sanitize_message' from 'llm.firewall_stack'`.

- [ ] **Step 3: Promote the helpers**

In `llm/firewall_stack.py`, add module-level functions ABOVE the `FirewallStack` class (delete the duplicates inside the class in the cleanup phase, not now):

```python
def sanitize_message(message: str) -> str:
    """Mask identifiers: long digit runs (6+ digits) and CASE-\\d+ tokens."""
    masked = _CASE_ID_RE.sub("[CASE-ID]", message)
    return _DIGIT_RUN_RE.sub("***MASKED***", masked)


def redact_payload(payload):
    if isinstance(payload, str):
        return sanitize_message(payload)
    if isinstance(payload, BaseModel):
        dumped = payload.model_dump()
        redacted = redact_payload(dumped)
        return type(payload).model_validate(redacted)
    if isinstance(payload, dict):
        return {k: redact_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(redact_payload(v) for v in payload)
    return payload
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_llm/test_firewall_helpers.py -v
pytest tests/test_llm/test_firewall_stack.py -v   # legacy test still passes
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add llm/firewall_stack.py tests/test_llm/test_firewall_helpers.py
git commit -m "feat: promote sanitize_message/redact_payload to public helpers"
```

---

### Task 1.2: `FirewalledAsyncOpenAI` — outbound message redaction

Build the wrapper class and verify outbound messages are redacted before `chat.completions.create` is called on the underlying client.

**Files:**
- Create: `llm/firewall_client.py`
- Test: `tests/test_llm/test_firewall_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm/test_firewall_client.py`:
```python
import pytest
from unittest.mock import AsyncMock
from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger

@pytest.mark.asyncio
async def test_outbound_messages_are_redacted():
    base = AsyncMock()
    base.chat.completions.create = AsyncMock(return_value="fake")
    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = FirewalledAsyncOpenAI(base=base, firewall=firewall)

    await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an assistant."},
            {"role": "user", "content": "Look up CASE-12345 and acct 1234567"},
        ],
    )

    sent = base.chat.completions.create.call_args.kwargs["messages"]
    assert sent[1]["content"] == "Look up [CASE-ID] and acct ***MASKED***"
```

- [ ] **Step 2: Run test, expect failure**

```bash
pytest tests/test_llm/test_firewall_client.py::test_outbound_messages_are_redacted -v
```
Expected: `ModuleNotFoundError: No module named 'llm.firewall_client'`.

- [ ] **Step 3: Implement minimal wrapper**

Create `llm/firewall_client.py`:
```python
"""FirewalledAsyncOpenAI — wraps openai.AsyncOpenAI with PII redaction,
retry-with-guidance on FirewallRejection, and a shared concurrency cap."""

from __future__ import annotations

from typing import Any

from llm.firewall_stack import FirewallStack, sanitize_message


def _redact_message(message: dict) -> dict:
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": sanitize_message(content)}
    return message


class _FirewalledChatCompletions:
    def __init__(self, base_completions: Any, firewall: FirewallStack):
        self._base = base_completions
        self._firewall = firewall

    async def create(self, *, model, messages, **kw):
        messages = [_redact_message(m) for m in messages]
        return await self._base.create(model=model, messages=messages, **kw)


class _FirewalledChat:
    def __init__(self, base_chat: Any, firewall: FirewallStack):
        self.completions = _FirewalledChatCompletions(base_chat.completions, firewall)


class FirewalledAsyncOpenAI:
    """Drop-in replacement for openai.AsyncOpenAI used by the Agents SDK."""

    def __init__(self, base: Any, firewall: FirewallStack):
        self._base = base
        self._firewall = firewall
        self.chat = _FirewalledChat(base.chat, firewall)

    def __getattr__(self, name: str):
        # Delegate all other endpoints (responses, files, etc.) to the base client.
        return getattr(self._base, name)
```

- [ ] **Step 4: Run test, expect pass**

```bash
pytest tests/test_llm/test_firewall_client.py::test_outbound_messages_are_redacted -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add llm/firewall_client.py tests/test_llm/test_firewall_client.py
git commit -m "feat(llm): FirewalledAsyncOpenAI with outbound redaction"
```

---

### Task 1.3: `FirewalledAsyncOpenAI` — retry-with-guidance on `FirewallRejection`

Add the retry loop. When the base client raises `FirewallRejection`, inject `FIREWALL_GUIDANCE` into the system message and retry up to `max_retries`.

**Files:**
- Modify: `llm/firewall_client.py`
- Modify: `tests/test_llm/test_firewall_client.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_llm/test_firewall_client.py`:
```python
from llm.firewall_stack import FirewallRejection, FIREWALL_GUIDANCE

@pytest.mark.asyncio
async def test_retry_with_guidance_on_firewall_rejection():
    base = AsyncMock()
    # First call raises FirewallRejection, second call succeeds.
    base.chat.completions.create = AsyncMock(side_effect=[
        FirewallRejection("PII", "blocked"),
        "ok",
    ])
    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = FirewalledAsyncOpenAI(base=base, firewall=firewall)

    result = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Original system prompt."},
            {"role": "user", "content": "user input"},
        ],
    )

    assert result == "ok"
    # Second call's system prompt has guidance appended.
    second_messages = base.chat.completions.create.call_args_list[1].kwargs["messages"]
    assert FIREWALL_GUIDANCE in second_messages[0]["content"]


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    base = AsyncMock()
    base.chat.completions.create = AsyncMock(side_effect=FirewallRejection("PII", "always"))
    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = FirewalledAsyncOpenAI(base=base, firewall=firewall)

    with pytest.raises(FirewallRejection):
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )
    # 1 original + 2 retries = 3 attempts
    assert base.chat.completions.create.call_count == 3
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_llm/test_firewall_client.py -v
```
Expected: the two new tests fail (no retry behavior yet).

- [ ] **Step 3: Implement retry-with-guidance**

Replace the `_FirewalledChatCompletions.create` method body in `llm/firewall_client.py`:
```python
from llm.firewall_stack import FIREWALL_GUIDANCE, FirewallRejection


def _inject_guidance(messages: list[dict]) -> list[dict]:
    """Append firewall guidance to the system message; resanitize all messages."""
    out = []
    appended = False
    for m in messages:
        m = _redact_message(m)
        if not appended and m.get("role") == "system":
            m = {**m, "content": (m.get("content") or "") + "\n\n" + FIREWALL_GUIDANCE}
            appended = True
        out.append(m)
    return out


class _FirewalledChatCompletions:
    def __init__(self, base_completions, firewall):
        self._base = base_completions
        self._firewall = firewall

    async def create(self, *, model, messages, **kw):
        messages = [_redact_message(m) for m in messages]
        attempt = 0
        while True:
            try:
                return await self._base.create(model=model, messages=messages, **kw)
            except FirewallRejection as e:
                self._firewall.logger.log("firewall_rejection",
                                          {"code": e.code, "message": e.message,
                                           "attempt": attempt})
                if attempt >= self._firewall.max_retries:
                    self._firewall.logger.log("firewall_blocked",
                                              {"code": e.code, "message": e.message,
                                               "attempts": attempt + 1})
                    raise
                attempt += 1
                messages = _inject_guidance(messages)
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_llm/test_firewall_client.py -v
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add llm/firewall_client.py tests/test_llm/test_firewall_client.py
git commit -m "feat(llm): retry-with-guidance on FirewallRejection"
```

---

### Task 1.4: `FirewalledAsyncOpenAI` — concurrency cap

Acquire `firewall.semaphore` around each request so total in-flight calls across all wrapped clients are capped.

**Files:**
- Modify: `llm/firewall_client.py`
- Modify: `tests/test_llm/test_firewall_client.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_llm/test_firewall_client.py`:
```python
import asyncio

@pytest.mark.asyncio
async def test_concurrency_cap_holds():
    in_flight = 0
    max_seen = 0
    gate = asyncio.Event()

    async def slow_create(**kw):
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await gate.wait()        # hold until released
        in_flight -= 1
        return "ok"

    base = AsyncMock()
    base.chat.completions.create = AsyncMock(side_effect=slow_create)
    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=0, concurrency_cap=2)
    client = FirewalledAsyncOpenAI(base=base, firewall=firewall)

    async def call():
        return await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )

    tasks = [asyncio.create_task(call()) for _ in range(5)]
    # Let the scheduler get them queued up.
    await asyncio.sleep(0.05)
    assert max_seen <= 2

    gate.set()
    await asyncio.gather(*tasks)
    assert max_seen == 2
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_llm/test_firewall_client.py::test_concurrency_cap_holds -v
```
Expected: assertion failure — concurrency unbounded.

- [ ] **Step 3: Add semaphore around the create call**

In `llm/firewall_client.py`, modify `_FirewalledChatCompletions.create` to acquire the firewall semaphore:
```python
async def create(self, *, model, messages, **kw):
    messages = [_redact_message(m) for m in messages]
    attempt = 0
    while True:
        try:
            async with self._firewall.semaphore:
                return await self._base.create(model=model, messages=messages, **kw)
        except FirewallRejection as e:
            self._firewall.logger.log("firewall_rejection",
                                      {"code": e.code, "message": e.message, "attempt": attempt})
            if attempt >= self._firewall.max_retries:
                self._firewall.logger.log("firewall_blocked",
                                          {"code": e.code, "message": e.message,
                                           "attempts": attempt + 1})
                raise
            attempt += 1
            messages = _inject_guidance(messages)
```

Also rename the field on `FirewallStack` from `_semaphore` to `semaphore` (public) so the wrapper can access it. Edit `llm/firewall_stack.py`:
- Change `self._semaphore = asyncio.Semaphore(concurrency_cap)` → `self.semaphore = asyncio.Semaphore(concurrency_cap)`
- Update any `self._semaphore` reference inside `FirewalledModel._tool_loop` to `self.semaphore` (the legacy `_tool_loop` is deleted in Phase 6, but keep it working until then)

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_llm/test_firewall_client.py -v
pytest tests/test_llm/ -v   # ensure legacy tests still pass
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add llm/firewall_client.py llm/firewall_stack.py tests/test_llm/test_firewall_client.py
git commit -m "feat(llm): shared concurrency cap on FirewalledAsyncOpenAI"
```

---

## Phase 2 — Tool layer

### Task 2.1: Decorate data tools with `@function_tool`

Keep the module-level `init_tools(gateway, catalog, logger)` pattern (avoids broad refactor of internal helpers like `_log_call`/`_log_result`). Add `@function_tool` to the three public functions.

**Files:**
- Modify: `tools/data_tools.py`
- Test: `tests/test_tools/test_data_tools_function_tool.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools/__init__.py` (empty) and `tests/test_tools/test_data_tools_function_tool.py`:
```python
"""Verify that data_tools functions are exposed as Agents SDK function_tools."""
from tools.data_tools import list_available_tables, get_table_schema, query_table


def test_list_available_tables_is_function_tool():
    # The @function_tool decorator wraps the callable; it remains callable
    # but exposes SDK-recognized metadata.
    assert hasattr(list_available_tables, "name")
    assert list_available_tables.name == "list_available_tables"


def test_get_table_schema_is_function_tool():
    assert hasattr(get_table_schema, "name")
    assert get_table_schema.name == "get_table_schema"


def test_query_table_is_function_tool():
    assert hasattr(query_table, "name")
    assert query_table.name == "query_table"
```

The exact attribute (`name` vs `tool_name` etc.) was confirmed by the Phase 0.3 spike — adjust the test if the spike showed a different attribute. If the SDK exposes `Tool` instances rather than callables, test for `isinstance(query_table, Tool)`.

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_tools/test_data_tools_function_tool.py -v
```
Expected: AttributeError — plain functions don't have `name`.

- [ ] **Step 3: Add the decorator**

In `tools/data_tools.py`, add at the top:
```python
from agents import function_tool
```

Then prepend `@function_tool` to each of the three public functions:
```python
@function_tool
def list_available_tables() -> str:
    ...

@function_tool
def get_table_schema(table_name: str) -> str:
    ...

@function_tool
def query_table(
    table_name: str,
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    columns: str = "",
) -> str:
    ...
```

Leave `init_tools`, `set_logger`, and the `_*` helpers undecorated — they're internal.

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_tools/ -v
pytest tests/  # full suite — confirm no regression in callers using these tools
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add tools/data_tools.py tests/test_tools/
git commit -m "feat(tools): decorate data_tools with @function_tool"
```

---

## Phase 3 — Agent factories (additive)

### Task 3.1: `build_specialist_agent` factory

**Files:**
- Create: `case_agents/specialist_agent.py`
- Test: `tests/test_case_agents/test_specialist_agent.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_case_agents/test_specialist_agent.py`:
```python
from agents import Agent
from case_agents.specialist_agent import build_specialist_agent
from models.types import DomainSkill, SpecialistOutput


def test_build_specialist_agent_returns_agent():
    skill = DomainSkill(
        name="creditrisk",
        system_prompt="You analyze credit risk.",
        data_hints=["bureau", "model_scores"],
        interpretation_guide="Use FICO < 580 as risky.",
        risk_signals=["delinquency", "high DTI"],
    )
    pillar = {"focus": "credit", "cut_off_date": "2025-12-01"}
    agent = build_specialist_agent(skill, pillar, model=None)

    assert isinstance(agent, Agent)
    assert agent.name == "creditrisk"
    assert agent.output_type is SpecialistOutput
    assert "You analyze credit risk." in agent.instructions
    assert "2025-12-01" in agent.instructions  # pillar overlay rendered
    assert len(agent.tools) == 3   # list_available_tables, get_table_schema, query_table
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_case_agents/test_specialist_agent.py -v
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement the factory**

Create `case_agents/specialist_agent.py`:
```python
"""Specialist Agent factory — replaces BaseSpecialistAgent under the SDK."""
from __future__ import annotations

from pathlib import Path

from agents import Agent

from models.types import DomainSkill, SpecialistOutput
from skills.loader import load_skill as _load_skill
from tools.data_tools import get_table_schema, list_available_tables, query_table

_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"
_BASE_INSTRUCTIONS = _load_skill(_WORKFLOW_DIR / "data_query.md").body


def _compose_instructions(skill: DomainSkill, pillar: dict) -> str:
    parts = [_BASE_INSTRUCTIONS,
             f"Domain: {skill.name}",
             f"Expertise: {skill.system_prompt}"]
    if skill.data_hints:
        parts.append(f"Data hints: {', '.join(skill.data_hints)}")
    if skill.interpretation_guide:
        parts.append(f"Interpretation guide: {skill.interpretation_guide}")
    if skill.risk_signals:
        parts.append(f"Risk signals: {', '.join(skill.risk_signals)}")
    if pillar:
        if "focus" in pillar:
            parts.append(f"Pillar focus: {pillar['focus']}")
        if "overlay" in pillar:
            parts.append(f"Pillar overlay: {pillar['overlay']}")
        if "cut_off_date" in pillar:
            cutoff = pillar["cut_off_date"]
            parts.append(
                f"DATA CUT-OFF DATE: {cutoff}\n"
                f"CRITICAL — Interpret ALL time-window language ('recent', 'current', "
                f"'last 3 months', 'this year') relative to this cut-off, NEVER relative "
                f"to today's calendar date. No data exists beyond {cutoff}."
            )
    return "\n\n".join(parts)


def build_specialist_agent(skill: DomainSkill, pillar: dict, model) -> Agent:
    return Agent(
        name=skill.name,
        instructions=_compose_instructions(skill, pillar),
        tools=[list_available_tables, get_table_schema, query_table],
        output_type=SpecialistOutput,
        model=model,
    )
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_case_agents/test_specialist_agent.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add case_agents/specialist_agent.py tests/test_case_agents/test_specialist_agent.py
git commit -m "feat(agents): build_specialist_agent factory"
```

---

### Task 3.2: `build_general_specialist` factory

**Files:**
- Modify: `case_agents/general_specialist.py` (add factory; leave the legacy class for now)
- Test: `tests/test_case_agents/test_general_specialist_factory.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_case_agents/test_general_specialist_factory.py`:
```python
from agents import Agent
from case_agents.general_specialist import build_general_specialist
from models.types import ReviewReport


def test_build_general_specialist_returns_agent():
    agent = build_general_specialist(model=None)
    assert isinstance(agent, Agent)
    assert agent.name == "general_specialist"
    assert agent.output_type is ReviewReport
    assert agent.tools == []
    assert "compare" in agent.instructions.lower() or "contradiction" in agent.instructions.lower()
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_case_agents/test_general_specialist_factory.py -v
```
Expected: `ImportError: cannot import name 'build_general_specialist'`.

- [ ] **Step 3: Add the factory**

Append to `case_agents/general_specialist.py` (don't remove the existing class — Phase 6 deletes it):
```python
from agents import Agent

# Reuse the existing comparator prompt that today's GeneralSpecialist class loads.
# Locate the constant in this file (e.g., COMPARE_PROMPT or similar) — read it,
# don't duplicate. If the prompt is built inside .compare(), extract it to a
# module-level constant first.

def build_general_specialist(model) -> Agent:
    return Agent(
        name="general_specialist",
        instructions=COMPARE_PROMPT,
        tools=[],
        output_type=ReviewReport,
        model=model,
    )
```

If the existing prompt isn't a module-level constant, refactor it into one named `COMPARE_PROMPT` (this is a small extract refactor that doesn't change behavior).

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_case_agents/test_general_specialist_factory.py -v
pytest tests/test_case_agents/test_general_specialist.py -v   # legacy class tests still pass
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add case_agents/general_specialist.py tests/test_case_agents/test_general_specialist_factory.py
git commit -m "feat(agents): build_general_specialist factory"
```

---

### Task 3.3: `build_report_agent` factory + `fs_*` tools with `RunContext`

The report agent reads the case folder. We thread `case_folder` via `RunContextWrapper` since it's per-request (unlike `gateway`, which is set once at session start).

**Files:**
- Modify: `case_agents/report_agent.py`
- Create: `tools/fs_tools.py`
- Test: `tests/test_case_agents/test_report_agent_factory.py`
- Test: `tests/test_tools/test_fs_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tools/test_fs_tools.py`:
```python
import pytest
from pathlib import Path
from agents import RunContextWrapper
from tools.fs_tools import fs_list_files, fs_read_file
from case_agents.app_context import AppContext


@pytest.mark.asyncio
async def test_fs_list_files_returns_files_in_case_folder(tmp_path):
    (tmp_path / "credit_review.md").write_text("content")
    (tmp_path / "summary.txt").write_text("more")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_list_files(ctx)
    assert "credit_review.md" in out
    assert "summary.txt" in out


@pytest.mark.asyncio
async def test_fs_read_file_reads_named_file(tmp_path):
    (tmp_path / "report.md").write_text("Top finding: X.")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file(ctx, "report.md")
    assert "Top finding: X." in out


@pytest.mark.asyncio
async def test_fs_read_file_rejects_path_traversal(tmp_path):
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file(ctx, "../etc/passwd")
    assert "denied" in out.lower() or "invalid" in out.lower()
```

Create `tests/test_case_agents/test_report_agent_factory.py`:
```python
from agents import Agent
from case_agents.report_agent import build_report_agent
from models.types import ReportDraft


def test_build_report_agent_returns_agent():
    agent = build_report_agent(model=None)
    assert isinstance(agent, Agent)
    assert agent.name == "report_agent"
    assert agent.output_type is ReportDraft
    assert len(agent.tools) == 2  # fs_list_files, fs_read_file
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_tools/test_fs_tools.py tests/test_case_agents/test_report_agent_factory.py -v
```
Expected: `ModuleNotFoundError` for both new modules.

- [ ] **Step 3: Implement**

Create `case_agents/app_context.py`:
```python
"""Per-request context object threaded through Runner.run for tools."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AppContext:
    gateway: Any
    case_folder: Path
    logger: Any
```

Create `tools/fs_tools.py`:
```python
"""Filesystem tools for the report agent. Confined to the active case folder."""
from __future__ import annotations

from pathlib import Path

from agents import RunContextWrapper, function_tool

from case_agents.app_context import AppContext


@function_tool
async def fs_list_files(ctx: RunContextWrapper[AppContext]) -> str:
    folder = ctx.context.case_folder
    if folder is None or not folder.exists():
        return "No case folder available."
    files = [p.name for p in folder.iterdir() if p.is_file()]
    return "\n".join(sorted(files)) if files else "Folder is empty."


@function_tool
async def fs_read_file(ctx: RunContextWrapper[AppContext], filename: str) -> str:
    folder = ctx.context.case_folder
    if folder is None:
        return "No case folder available."
    target = (folder / filename).resolve()
    # Confine to case_folder to prevent path traversal.
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return f"Access denied: '{filename}' is outside the case folder."
    if not target.exists() or not target.is_file():
        return f"File not found: {filename}"
    return target.read_text()
```

Append to `case_agents/report_agent.py` (leave the legacy `ReportAgent` class for Phase 6 deletion):
```python
from agents import Agent
from tools.fs_tools import fs_list_files, fs_read_file

REPORT_AGENT_INSTRUCTIONS = """\
You are the Report Agent. Search the case folder for prior curated reports
and extract findings relevant to the question. Use fs_list_files to discover
files; use fs_read_file to read any markdown or text file.
Return a ReportDraft with: answer (synthesized from prior reports),
coverage ('full', 'partial', 'none'), evidence_excerpts (quoted lines),
and files_consulted (list of filenames you read).
"""


def build_report_agent(model) -> Agent:
    return Agent(
        name="report_agent",
        instructions=REPORT_AGENT_INSTRUCTIONS,
        tools=[fs_list_files, fs_read_file],
        output_type=ReportDraft,
        model=model,
    )
```

If the existing legacy class has a more thorough prompt, port that text into `REPORT_AGENT_INSTRUCTIONS` instead of using the placeholder above.

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_tools/test_fs_tools.py tests/test_case_agents/test_report_agent_factory.py -v
pytest tests/  # ensure legacy report agent tests still pass
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add case_agents/app_context.py case_agents/report_agent.py tools/fs_tools.py tests/
git commit -m "feat(agents): build_report_agent factory + fs_tools"
```

---

### Task 3.4: `redacting_tool` helper for inter-agent transit redaction

Wraps `agent.as_tool()` to apply input/output redaction at the orchestrator→specialist boundary (spec §4.3 boundaries 1+2).

**Files:**
- Create: `case_agents/redacting_tool.py`
- Test: `tests/test_case_agents/test_redacting_tool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_case_agents/test_redacting_tool.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from case_agents.redacting_tool import redacting_tool


@pytest.mark.asyncio
async def test_redacting_tool_sanitizes_input_to_inner_agent():
    inner_agent = MagicMock()
    raw_tool = MagicMock()
    raw_tool.invoke = AsyncMock(return_value={"answer": "ok"})
    inner_agent.as_tool = MagicMock(return_value=raw_tool)

    wrapped = redacting_tool(inner_agent, name="x", description="d")
    # Invoke the wrapper with a payload containing a CASE-ID
    await wrapped.invoke("Investigate CASE-12345")

    # The inner tool received the redacted form
    raw_tool.invoke.assert_awaited_once_with("Investigate [CASE-ID]")


@pytest.mark.asyncio
async def test_redacting_tool_redacts_output():
    inner_agent = MagicMock()
    raw_tool = MagicMock()
    raw_tool.invoke = AsyncMock(return_value={"answer": "Found CASE-99999 issue"})
    inner_agent.as_tool = MagicMock(return_value=raw_tool)

    wrapped = redacting_tool(inner_agent, name="x", description="d")
    out = await wrapped.invoke("anything")

    assert out == {"answer": "Found [CASE-ID] issue"}
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_case_agents/test_redacting_tool.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement** (the SDK's `as_tool` invocation API was confirmed in Task 0.3 — adjust below if the spike showed a different shape)

Create `case_agents/redacting_tool.py`:
```python
"""Wraps agent.as_tool() with PII redaction on tool input + output."""
from __future__ import annotations

from agents import Agent

from llm.firewall_stack import redact_payload, sanitize_message


def redacting_tool(agent: Agent, name: str, description: str):
    """Return a tool that redacts inputs before forwarding to ``agent`` and
    redacts the agent's output before returning it to the caller."""
    raw_tool = agent.as_tool(tool_name=name, tool_description=description)

    # The exact wrapping pattern depends on the SDK's Tool object shape.
    # Pattern A (Tool exposes .invoke that takes the raw input string):
    original_invoke = raw_tool.invoke

    async def wrapped_invoke(input_str: str, *args, **kwargs):
        redacted_input = sanitize_message(input_str)
        result = await original_invoke(redacted_input, *args, **kwargs)
        return redact_payload(result)

    raw_tool.invoke = wrapped_invoke  # monkey-patch the wrapper onto the Tool
    return raw_tool
```

If the SDK's `Tool` is immutable or `as_tool()` returns a callable rather than an object, switch to a `@function_tool`-decorated wrapper that internally calls `Runner.run(agent, ...)`. Use whichever pattern the Phase 0.3 spike confirmed.

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_case_agents/test_redacting_tool.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add case_agents/redacting_tool.py tests/test_case_agents/test_redacting_tool.py
git commit -m "feat(agents): redacting_tool helper for inter-agent transit"
```

---

### Task 3.5: `build_orchestrator_agent` factory

**Files:**
- Create: `case_agents/orchestrator_agent.py`
- Test: `tests/test_case_agents/test_orchestrator_agent.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_case_agents/test_orchestrator_agent.py`:
```python
from agents import Agent
from case_agents.orchestrator_agent import build_orchestrator_agent
from case_agents.specialist_agent import build_specialist_agent
from case_agents.general_specialist import build_general_specialist
from case_agents.report_agent import build_report_agent
from models.types import DomainSkill, FinalAnswer


def test_build_orchestrator_agent_wires_all_tools():
    skill_a = DomainSkill(name="creditrisk", system_prompt="x", data_hints=[],
                          interpretation_guide="", risk_signals=[])
    skill_b = DomainSkill(name="taxcompliance", system_prompt="y", data_hints=[],
                          interpretation_guide="", risk_signals=[])
    specialists = [build_specialist_agent(skill_a, {}, model=None),
                   build_specialist_agent(skill_b, {}, model=None)]
    report = build_report_agent(model=None)
    general = build_general_specialist(model=None)

    agent = build_orchestrator_agent(specialists, report, general, model=None)

    assert isinstance(agent, Agent)
    assert agent.name == "orchestrator"
    assert agent.output_type is FinalAnswer
    # 2 specialists + report_agent + general_specialist = 4 tools
    assert len(agent.tools) == 4
    # Instructions absorb the four workflow skills
    for keyword in ["specialist", "synthes", "balanc"]:
        assert keyword in agent.instructions.lower()
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_case_agents/test_orchestrator_agent.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `case_agents/orchestrator_agent.py`:
```python
"""Orchestrator Agent factory — A1 maximal: specialists + report + general as tools."""
from __future__ import annotations

from pathlib import Path

from agents import Agent

from case_agents.redacting_tool import redacting_tool
from models.types import FinalAnswer
from skills.loader import load_skill as _load_skill

_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


def _compose_orchestrator_instructions() -> str:
    parts = [
        _load_skill(_WORKFLOW_DIR / "team_construction.md").body,
        _load_skill(_WORKFLOW_DIR / "data_catalog.md").body,
        _load_skill(_WORKFLOW_DIR / "synthesis.md").body,
        _load_skill(_WORKFLOW_DIR / "balancing.md").body,
        (
            "PARALLEL EXECUTION: When multiple specialists are needed, emit ALL "
            "tool calls in a single response so they execute in parallel. Do not "
            "serialize specialist calls."
        ),
    ]
    return "\n\n---\n\n".join(parts)


def _describe_specialist(agent: Agent) -> str:
    return f"Domain specialist '{agent.name}' — call with a focused sub-question."


def build_orchestrator_agent(
    specialists: list[Agent],
    report_agent: Agent,
    general_specialist: Agent,
    model,
) -> Agent:
    tools = [
        redacting_tool(s, name=s.name, description=_describe_specialist(s))
        for s in specialists
    ]
    tools.append(redacting_tool(
        report_agent,
        name="report_agent",
        description="Look up prior curated reports for this case.",
    ))
    tools.append(redacting_tool(
        general_specialist,
        name="general_specialist",
        description="Compare specialist outputs and surface contradictions.",
    ))

    return Agent(
        name="orchestrator",
        instructions=_compose_orchestrator_instructions(),
        tools=tools,
        output_type=FinalAnswer,
        model=model,
    )
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_case_agents/test_orchestrator_agent.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add case_agents/orchestrator_agent.py tests/test_case_agents/test_orchestrator_agent.py
git commit -m "feat(agents): build_orchestrator_agent factory"
```

---

## Phase 4 — Orchestrator wiring

### Task 4.1: New `Orchestrator.__init__` builds the agent graph

Replace `Orchestrator.__init__` to construct the agent graph once per session. Keep the external signature; `registry` becomes optional and is ignored (caller cleanup happens in Task 5.1).

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Create: `llm/factory.py` rewrite (`build_session_clients`)
- Test: `tests/test_orchestrator_init.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_init.py`:
```python
from unittest.mock import MagicMock
from openai import AsyncOpenAI
from llm.factory import build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from orchestrator.orchestrator import Orchestrator


def test_orchestrator_constructs_agent_graph():
    logger = EventLogger(session_id="t")
    firewall = FirewallStack(logger, max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=MagicMock(spec=AsyncOpenAI))

    orch = Orchestrator(
        llm=None,                    # legacy field tolerated; new path uses clients
        logger=logger,
        registry=None,
        pillar="credit",
        pillar_config={},
        catalog=MagicMock(),
        gateway=MagicMock(),
        clients=clients,
    )
    assert orch.orchestrator_agent is not None
    assert orch.orchestrator_agent.name == "orchestrator"
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_orchestrator_init.py -v
```
Expected: `ImportError: cannot import name 'build_session_clients'` or AttributeError on `orch.orchestrator_agent`.

- [ ] **Step 3: Implement**

Rewrite `llm/factory.py`:
```python
"""Session client factory — builds the firewalled AsyncOpenAI + the SDK Model."""
from __future__ import annotations

from dataclasses import dataclass

from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack


@dataclass
class SessionClients:
    firewalled_client: FirewalledAsyncOpenAI
    model: OpenAIChatCompletionsModel


def build_session_clients(
    firewall: FirewallStack,
    *,
    model_name: str = "gpt-4o",
    base_client: AsyncOpenAI | None = None,
) -> SessionClients:
    base = base_client or AsyncOpenAI()
    firewalled = FirewalledAsyncOpenAI(base=base, firewall=firewall)
    model = OpenAIChatCompletionsModel(model=model_name, openai_client=firewalled)
    return SessionClients(firewalled_client=firewalled, model=model)
```

Modify `orchestrator/orchestrator.py` `__init__` to accept `clients` and build the agent graph (preserve all existing fields for backward compatibility during migration):
```python
from skills.domain.loader import list_domain_skills, load_domain_skill
from case_agents.orchestrator_agent import build_orchestrator_agent
from case_agents.specialist_agent import build_specialist_agent
from case_agents.general_specialist import build_general_specialist
from case_agents.report_agent import build_report_agent

class Orchestrator:
    def __init__(
        self,
        llm,                  # legacy — ignored when clients is provided
        logger,
        registry=None,        # legacy — ignored
        pillar="credit",
        pillar_config=None,
        catalog=None,
        gateway=None,
        clients=None,
    ):
        self.llm = llm
        self.logger = logger
        self.registry = registry
        self.pillar = pillar
        self.pillar_config = pillar_config or {}
        self.catalog = catalog
        self.gateway = gateway
        self.clients = clients

        # Build the SDK agent graph if clients are provided. (When legacy
        # callers haven't been updated yet, agent graph is None and the
        # legacy Orchestrator.run path must still work — this is removed in
        # Task 5.1 once main.py is migrated.)
        if clients is not None:
            domain_names = list_domain_skills()
            specialists = [
                build_specialist_agent(load_domain_skill(d), self.pillar_config,
                                       model=clients.model)
                for d in domain_names if load_domain_skill(d) is not None
            ]
            self.report_agent_obj = build_report_agent(model=clients.model)
            self.general_agent = build_general_specialist(model=clients.model)
            self.orchestrator_agent = build_orchestrator_agent(
                specialists=specialists,
                report_agent=self.report_agent_obj,
                general_specialist=self.general_agent,
                model=clients.model,
            )
        else:
            self.orchestrator_agent = None
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_orchestrator_init.py -v
pytest tests/test_main_resolver.py -v   # ensure legacy callers still work
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py llm/factory.py tests/test_orchestrator_init.py
git commit -m "feat(orchestrator): build SDK agent graph in __init__"
```

---

### Task 4.2: New `Orchestrator.run` happy path

Replace the body of `run()` with `Runner.run` on the orchestrator agent. Keep the external signature.

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Test: `tests/test_orchestrator_run.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_run.py`:
```python
"""End-to-end smoke: real Runner, mocked AsyncOpenAI."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from llm.factory import build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from models.types import FinalAnswer
from orchestrator.orchestrator import Orchestrator


def _final_answer_response(answer: str) -> ChatCompletion:
    """Construct a mock OpenAI response with a structured JSON output."""
    payload = json.dumps({
        "answer": answer,
        "flags": [],
        "report_draft": None,
        "team_draft": None,
    })
    # Simplified mock — adjust attributes to match what the SDK reads.
    msg = MagicMock()
    msg.content = payload
    msg.tool_calls = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(spec=ChatCompletion); resp.choices = [choice]
    return resp


@pytest.mark.asyncio
async def test_orchestrator_run_returns_final_answer(tmp_path):
    base = AsyncMock(spec=AsyncOpenAI)
    base.chat.completions.create = AsyncMock(
        return_value=_final_answer_response("Synthesized answer.")
    )

    logger = EventLogger(session_id="t")
    firewall = FirewallStack(logger, max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=base)

    orch = Orchestrator(
        llm=None, logger=logger, registry=None, pillar="credit",
        pillar_config={}, catalog=MagicMock(), gateway=MagicMock(),
        clients=clients,
    )
    result = await orch.run(
        question="Is this case high risk?",
        case_folder=tmp_path,
        report_agent=None,  # legacy arg — ignored under new path
    )
    assert isinstance(result, FinalAnswer)
    assert result.answer == "Synthesized answer."
```

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_orchestrator_run.py -v
```
Expected: failure (legacy `run` doesn't use `Runner.run`).

- [ ] **Step 3: Replace `Orchestrator.run` body**

In `orchestrator/orchestrator.py`, replace the existing `async def run(self, question, case_folder, report_agent) -> FinalAnswer` body with:
```python
from agents import Runner
from case_agents.app_context import AppContext
from llm.firewall_stack import redact_payload

async def run(self, question, case_folder, report_agent=None) -> FinalAnswer:
    self.logger.log("orchestrator_run_start",
                    {"question": question, "case_folder": str(case_folder)})
    ctx = AppContext(gateway=self.gateway, case_folder=case_folder, logger=self.logger)
    result = await Runner.run(self.orchestrator_agent, question, context=ctx)
    final = redact_payload(result.final_output)
    self.logger.log("orchestrator_run_done",
                    {"flag_count": len(final.flags),
                     "answer_len": len(final.answer)})
    return final
```

Delete the old `_run_team_workflow`, `plan_team`, `_select_team`, `_split_sub_questions`, `synthesize`, `balance`, `_balance_fallback`, `_parse_*` helpers — all replaced by the orchestrator agent's instructions. Delete `_build_specialist_descriptions`, `_build_case_schema`, `_case_aware_columns` (their logic now lives in `data_catalog.md` consumed by the orchestrator agent's instructions).

- [ ] **Step 4: Delete legacy orchestrator-internal tests in the same commit**

Find tests that pin the now-deleted internal methods:
```bash
grep -rln "plan_team\|_select_team\|_split_sub_questions\|_run_team_workflow\|_balance_fallback\|orchestrator\.synthesize\|orchestrator\.balance" tests/ --include="*.py"
```

Delete or trim each match:
- If the file ONLY tests these internals: `git rm <file>`
- If the file mixes internal and external tests: delete only the internal-method tests; keep the rest

Run `pytest tests/ -q` and confirm all green.

Per spec §7-3 (resolved before plan), the legacy orchestrator emitted events `plan_team_start`/`plan_team_done`, `select_team_done`, `select_team_fallback`, `split_sub_questions_fallback`, `specialist_invoked`, `specialist_reused`, `orchestrator_synthesize`. Under A1 these stages no longer exist as Python steps. We accept that they no longer fire. The events that DO still fire from `Orchestrator.run`: `orchestrator_run_start`, `orchestrator_run_done`, `data_pull_requested` (latter only when the orchestrator agent emits a `data_pull_request` field on `FinalAnswer`). If any test pinned the old event names, delete those assertions.

- [ ] **Step 5: Run full suite + commit**

```bash
pytest tests/ -q
```
Expected: all green.

```bash
git add -A
git commit -m "feat(orchestrator): replace run() with SDK Runner.run"
```

---

### Task 4.3: β trace-extraction fallback

Catch the SDK's retry-exhaustion exception and rebuild a `FinalAnswer` from completed tool outputs.

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Test: `tests/test_orchestrator_balance_fallback.py`

(Use the exact exception class name and trace-attribute name resolved in Task 0.3. Below uses `MaxTurnsExceeded` and `e.run_data.new_items` as placeholders — replace per spike findings.)

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_balance_fallback.py`:
```python
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from openai import AsyncOpenAI
from llm.factory import build_session_clients
from llm.firewall_stack import FirewallStack, FirewallRejection
from logger.event_logger import EventLogger
from models.types import FinalAnswer
from orchestrator.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_balance_fallback_recovers_partial_drafts(tmp_path):
    base = AsyncMock(spec=AsyncOpenAI)
    # First call: orchestrator emits a tool call to report_agent (succeeds, returns ReportDraft).
    # Second call: orchestrator's final synthesis trips FirewallRejection until exhaustion.
    base.chat.completions.create = AsyncMock(
        side_effect=[
            # Mock the SDK turn that returns a tool_call for report_agent → completes
            _mock_tool_call_response("report_agent", '{"answer":"Prior report says X","coverage":"full","evidence_excerpts":[],"files_consulted":["report.md"]}'),
            # Mock the final synthesis turn — always blocked
            FirewallRejection("PII", "blocked"),
            FirewallRejection("PII", "blocked"),
            FirewallRejection("PII", "blocked"),
        ]
    )

    logger = EventLogger(session_id="t")
    firewall = FirewallStack(logger, max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=base)
    orch = Orchestrator(
        llm=None, logger=logger, registry=None, pillar="credit",
        pillar_config={}, catalog=MagicMock(), gateway=MagicMock(),
        clients=clients,
    )

    result = await orch.run("question", tmp_path, report_agent=None)

    assert isinstance(result, FinalAnswer)
    assert "Prior report says X" in result.answer
    assert any("balancing fallback" in f for f in result.flags)


def _mock_tool_call_response(tool_name: str, output_json: str):
    # Detailed shape depends on the SDK; use spike findings.
    raise NotImplementedError("Fill in per Task 0.3 spike output")
```

(The mock helpers depend on Phase 0.3 findings; replace `_mock_tool_call_response` with a real shape.)

- [ ] **Step 2: Run, expect failure**

```bash
pytest tests/test_orchestrator_balance_fallback.py -v
```
Expected: failure (no fallback wired).

- [ ] **Step 3: Implement the fallback**

In `orchestrator/orchestrator.py`, wrap the `Runner.run` call:
```python
# Adjust to match Phase 0.3 findings
from agents.exceptions import MaxTurnsExceeded  # placeholder import
from agents import ToolCallOutputItem            # placeholder import
import json

async def run(self, question, case_folder, report_agent=None) -> FinalAnswer:
    self.logger.log("orchestrator_run_start", {...})
    ctx = AppContext(gateway=self.gateway, case_folder=case_folder, logger=self.logger)
    try:
        result = await Runner.run(self.orchestrator_agent, question, context=ctx)
        final = redact_payload(result.final_output)
    except (MaxTurnsExceeded, FirewallRejection) as e:
        final = self._trace_extraction_fallback(e)
    self.logger.log("orchestrator_run_done", {...})
    return final


def _trace_extraction_fallback(self, exc) -> FinalAnswer:
    # Per Phase 0.3 spike: locate completed ToolCallOutputItems on `exc`.
    items = getattr(getattr(exc, "run_data", None), "new_items", []) or []
    completed = [i for i in items if isinstance(i, ToolCallOutputItem)]

    report_draft_json = next((i.output for i in completed if i.tool_name == "report_agent"), None)
    specialist_outputs_json = [i.output for i in completed
                               if i.tool_name not in ("report_agent", "general_specialist")]

    if report_draft_json is None and not specialist_outputs_json:
        return FinalAnswer(
            answer="Analysis was blocked by content firewall after retries.",
            flags=["orchestrator blocked, no partial drafts recovered"],
        )

    parts = []
    if report_draft_json:
        try:
            rd = json.loads(report_draft_json)
            parts.append(f"[From curated reports]\n{rd.get('answer', '')}")
        except json.JSONDecodeError:
            pass
    if specialist_outputs_json:
        spec_blob = "\n".join(json.loads(s).get("findings", "")
                              for s in specialist_outputs_json
                              if _safe_json(s))
        if spec_blob:
            parts.append(f"[From team specialists]\n{spec_blob}")

    return FinalAnswer(
        answer="\n\n".join(parts) if parts else "Analysis blocked.",
        flags=["balancing fallback: orchestrator blocked"],
    )


def _safe_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False
```

- [ ] **Step 4: Run tests, expect pass**

```bash
pytest tests/test_orchestrator_balance_fallback.py -v
pytest tests/test_orchestrator_run.py -v
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/orchestrator.py tests/test_orchestrator_balance_fallback.py
git commit -m "feat(orchestrator): trace-extraction fallback on Runner exhaustion"
```

---

## Phase 5 — Caller updates

### Task 5.1: Update `main.py` — remove SessionRegistry, pass `clients`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Identify changes needed**

```bash
grep -n "SessionRegistry\|build_llm\|FirewalledModel" main.py
```
Expected: lines using legacy session-registry construction and `build_llm`.

- [ ] **Step 2: Update construction**

In `main.py`, replace the LLM-and-registry construction block with:
```python
from llm.factory import build_session_clients
from llm.firewall_stack import FirewallStack

# (remove: from agents.session_registry import SessionRegistry)
# (remove: from llm.factory import build_llm)

logger = EventLogger(session_id=session_id)
firewall = FirewallStack(logger, max_retries=2, concurrency_cap=8)
clients = build_session_clients(firewall, model_name="gpt-4o")

orchestrator = Orchestrator(
    llm=None,
    logger=logger,
    registry=None,
    pillar=pillar,
    pillar_config=pillar_config,
    catalog=catalog,
    gateway=gateway,
    clients=clients,
)
```

Also remove the line that builds a separate `report_agent` for injection — `report_agent` is now built inside `Orchestrator.__init__` and the legacy `run(report_agent=...)` arg is ignored.

- [ ] **Step 3: Run end-to-end smoke**

```bash
pytest tests/test_main_resolver.py -v
```
Expected: PASS.

- [ ] **Step 4: Manual smoke run**

```bash
OPENAI_API_KEY=$OPENAI_API_KEY python main.py --pillar credit --case CASE-12345 --question "Summarize this case."
```
Expected: produces a `FinalAnswer` printed to stdout (or whatever main.py does today).

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "refactor(main): wire build_session_clients, drop SessionRegistry"
```

---

### Task 5.2: Verify `ChatAgent` integration

ChatAgent's public methods (`screen`, `redact`, `relevance_check`, `converse`) should keep working. They use `self.llm.ainvoke(...)` — but `llm` was the old `FirewalledModel`. Decide: does ChatAgent still use a `FirewalledModel` for its own short LLM calls, or does it switch to a small SDK Agent?

**Files:**
- Read-only inspection first; modify only if necessary

- [ ] **Step 1: Read ChatAgent's LLM call sites**

```bash
grep -n "self\.llm\." case_agents/chat_agent.py
```

- [ ] **Step 2: Decide migration scope for ChatAgent**

Per spec §8 (out of scope): "Migrating `ChatAgent` to be an SDK `Agent`" is out of scope. So: keep ChatAgent's `self.llm.ainvoke(...)` working by giving it a thin shim — a function (or tiny class) that takes `system_prompt`, `user_message`, `output_type` and calls the firewalled `AsyncOpenAI` client directly.

- [ ] **Step 3: Add a minimal shim in `llm/factory.py`**

Append to `llm/factory.py`:
```python
class FirewalledChatShim:
    """Minimal LLMResult-style shim for ChatAgent's existing call sites."""
    def __init__(self, clients: SessionClients, model_name: str = "gpt-4o"):
        self._clients = clients
        self._model = model_name
        self.firewall = clients.firewalled_client._firewall  # for legacy access

    async def ainvoke(self, system_prompt, user_message, tools=None, output_type=None):
        from models.types import LLMResult
        resp = await self._clients.firewalled_client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        content = resp.choices[0].message.content
        if output_type is not None:
            try:
                import json as _json
                data = output_type(**_json.loads(content)).model_dump()
            except Exception:
                data = {"raw": content}
        else:
            data = {"response": content}
        return LLMResult(status="success", data=data)
```

In `main.py`, where ChatAgent is constructed, pass the shim as its `llm`:
```python
chat_llm = FirewalledChatShim(clients)
chat_agent = ChatAgent(llm=chat_llm, ...)
```

- [ ] **Step 4: Run ChatAgent tests**

```bash
pytest tests/test_case_agents/test_chat_agent.py -v
pytest tests/  # full suite
```
Expected: ChatAgent tests pass.

- [ ] **Step 5: Commit**

```bash
git add llm/factory.py main.py
git commit -m "feat(llm): FirewalledChatShim preserves ChatAgent's ainvoke surface"
```

---

## Phase 6 — Cleanup

### Task 6.1: Delete `BaseSpecialistAgent`

**Files:**
- Delete: `case_agents/base_agent.py`
- Delete: `tests/test_case_agents/test_base_agent.py`
- Modify: any remaining imports

- [ ] **Step 1: Confirm no live importers**

```bash
grep -rn "BaseSpecialistAgent\|from case_agents.base_agent\|from case_agents import base_agent" \
  --include="*.py" .
```
Expected: only references in `tests/test_case_agents/test_base_agent.py` and possibly `case_agents/__init__.py`.

- [ ] **Step 2: Run pre-deletion test sanity**

```bash
pytest tests/ -q
```
Note any failures unrelated to base_agent.

- [ ] **Step 3: Delete files and clean __init__**

```bash
git rm case_agents/base_agent.py tests/test_case_agents/test_base_agent.py
```

Open `case_agents/__init__.py` and remove any `from .base_agent import ...` line.

- [ ] **Step 4: Run tests**

```bash
pytest tests/ -q
```
Expected: same set of failures as Step 2 minus base-agent tests; nothing new broken.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: delete BaseSpecialistAgent (replaced by build_specialist_agent)"
```

---

### Task 6.2: Delete `SessionRegistry`

**Files:**
- Delete: `case_agents/session_registry.py`
- Delete: `tests/test_case_agents/test_session_registry.py`

- [ ] **Step 1: Confirm no live importers**

```bash
grep -rn "SessionRegistry" --include="*.py" .
```
Expected: only in the test file and possibly stale imports in `__init__.py` or `main.py`.

- [ ] **Step 2: Remove any remaining import lines**

```bash
grep -rln "SessionRegistry" --include="*.py" . | xargs sed -i '' '/SessionRegistry/d'
```

- [ ] **Step 3: Delete files**

```bash
git rm case_agents/session_registry.py tests/test_case_agents/test_session_registry.py
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/ -q
```
Expected: no SessionRegistry-related ImportError; tests as before.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: delete SessionRegistry (specialists are stateless under A1)"
```

---

### Task 6.3: Delete `FirewalledModel` and shrink `FirewallStack`

**Files:**
- Modify: `llm/firewall_stack.py`
- Delete: `tests/test_llm/test_firewalled_model.py`
- Modify: `tests/test_llm/test_firewall_stack.py`

- [ ] **Step 1: Confirm no live importers of `FirewalledModel`**

```bash
grep -rn "FirewalledModel\|FirewallStack\.wrap\|firewall\.wrap\|_tool_loop" --include="*.py" .
```
Expected: only in the legacy test file and the class itself.

- [ ] **Step 2: Delete legacy class and methods**

In `llm/firewall_stack.py`, delete:
- `class FirewalledModel` (the entire class)
- `FirewallStack.wrap`, `FirewallStack.send`, `FirewallStack._redact_payload`, `FirewallStack._sanitize_message`, `FirewallStack.rollback_to`
- `step_history` field
- `StepRecord` import if unused
- `_tool_loop` (was on FirewalledModel)
- `langchain_core` and other LangChain imports

Keep:
- `FirewallRejection` exception class
- `FIREWALL_GUIDANCE` constant
- `FirewallStack` class with only `__init__(logger, max_retries, semaphore)` and `logger` / `max_retries` / `semaphore` fields
- Module-level `sanitize_message`, `redact_payload` functions (from Task 1.1)

The shrunk `FirewallStack` should look roughly:
```python
class FirewallStack:
    def __init__(self, logger, max_retries: int = 2, concurrency_cap: int = 8):
        self.logger = logger
        self.max_retries = max_retries
        self.semaphore = asyncio.Semaphore(concurrency_cap)
```

- [ ] **Step 3: Delete and update tests**

```bash
git rm tests/test_llm/test_firewalled_model.py
```

Rewrite `tests/test_llm/test_firewall_stack.py` to test only the shrunk surface:
```python
import asyncio
import pytest
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger


def test_firewall_stack_holds_state():
    logger = EventLogger(session_id="t")
    fw = FirewallStack(logger, max_retries=3, concurrency_cap=5)
    assert fw.max_retries == 3
    assert isinstance(fw.semaphore, asyncio.Semaphore)
    assert fw.logger is logger
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_llm/ -v
pytest tests/ -q
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add llm/firewall_stack.py tests/test_llm/
git commit -m "refactor(llm): shrink FirewallStack, delete FirewalledModel"
```

---

### Task 6.4: Remove `langchain-*` from `requirements.txt`

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Confirm no remaining LangChain imports**

```bash
grep -rn "from langchain\|import langchain" --include="*.py" .
```
Expected: no results.

- [ ] **Step 2: Remove dependencies**

Delete from `requirements.txt`:
```
langchain-core>=0.3.0,<0.4.0
langchain-openai>=0.2.0,<0.3.0
```

- [ ] **Step 3: Reinstall and verify**

```bash
pip uninstall -y langchain-core langchain-openai
pip install -r requirements.txt
pytest tests/ -q
```
Expected: all tests pass; LangChain no longer installed.

- [ ] **Step 4: Run full smoke**

```bash
OPENAI_API_KEY=$OPENAI_API_KEY python main.py --pillar credit --case CASE-12345 --question "Quick smoke check."
```
Expected: produces a final answer; no errors.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "chore: remove langchain-* dependencies"
```

---

## Phase 7 — Cleanup of legacy agent classes

These three agent files contain both a legacy class AND the new factory function (added in Phase 3). Delete the legacy classes now that nothing references them.

### Task 7.1: Strip legacy classes from `case_agents/report_agent.py`, `general_specialist.py`, `data_manager_agent.py`

**Files:**
- Modify: `case_agents/report_agent.py`
- Modify: `case_agents/general_specialist.py`
- Modify: `case_agents/data_manager_agent.py`
- Modify/Delete: corresponding legacy tests

- [ ] **Step 1: Confirm legacy classes have no live importers**

```bash
grep -rn "ReportAgent\b\|GeneralSpecialist\b\|DataManagerAgent\b" --include="*.py" . \
  | grep -v "build_report_agent\|build_general_specialist\|build_data_manager"
```
Expected: results only in the class definitions and their tests.

- [ ] **Step 2: Delete the legacy classes**

In each of `case_agents/report_agent.py`, `general_specialist.py`, `data_manager_agent.py`:
- Delete the `class ReportAgent` / `class GeneralSpecialist` / `class DataManagerAgent` body
- Keep the new `build_*` factory function and any module-level constants the factory uses
- Remove now-unused imports (e.g., `FirewalledModel`)

If `data_manager_agent.py` is still using a different pattern (it sync-touches data tables; check whether it should also be migrated to a factory), that's a separate plan — for this migration, leave it alone if it's not invoked from the orchestrator path.

- [ ] **Step 3: Update the legacy tests**

Tests at `tests/test_case_agents/test_report_agent.py`, `test_general_specialist.py`, `test_data_manager_agent.py` likely test the deleted classes. Either:
- Delete the test file if it ONLY tested the class
- Keep and update if it also covers the factory

Do this per-file: read each test file, decide.

- [ ] **Step 4: Run tests**

```bash
pytest tests/ -q
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: delete legacy ReportAgent/GeneralSpecialist classes"
```

---

## Acceptance verification

- [ ] **Run full suite**

```bash
pytest -q
```
Expected: all tests pass.

- [ ] **Verify acceptance criteria from spec §9**

```bash
# 1. langchain-* removed
grep -E "^(langchain-core|langchain-openai)" requirements.txt
# expected: empty

# 2. Deleted classes are really gone
grep -rn "class BaseSpecialistAgent\|class SessionRegistry\|class FirewalledModel" --include="*.py" .
# expected: empty

# 3. External signature preserved
grep -n "def run" orchestrator/orchestrator.py
# expected: matches "async def run(self, question, case_folder, report_agent" (report_agent kept as legacy positional even if unused)
```

- [ ] **End-to-end smoke run**

```bash
OPENAI_API_KEY=$OPENAI_API_KEY python main.py --pillar credit --case CASE-12345 --question "End-to-end migration smoke."
```
Expected: produces a `FinalAnswer` and exits cleanly.

---

## Self-review notes (for the implementer)

- **Phase 0.1 sed command on Linux:** drop the `''` after `-i` (BSD vs GNU sed difference).
- **`@function_tool` attribute name:** Tests in 2.1 use `tool.name`; verify against Phase 0.3 spike output. If the SDK exposes `tool.tool_name` or only the class name, adjust.
- **`agent.as_tool()` return shape:** Task 3.4 monkey-patches `raw_tool.invoke`. If the spike showed Tool objects are immutable or the invocation contract differs, switch the wrapper to a `@function_tool` that internally calls `Runner.run(agent, ...)`.
- **`MaxTurnsExceeded` and `ToolCallOutputItem`:** Placeholders in Task 4.3 — replace with the exact names found in Phase 0.3.
- **`OPENAI_API_KEY` for spike (Task 0.3) and smoke runs (5.1, 6.4, acceptance):** ensure it's set in your environment.
- **Pillar config in Task 4.1:** the current code has `_build_case_schema` reading the gateway's case tables and threading them into specialist descriptions. Under A1, this logic is consumed by the orchestrator agent's instructions through `data_catalog.md`. If the new orchestrator's tool descriptions need a richer case-aware schema, build a small helper in `case_agents/orchestrator_agent.py` that injects a "current case schema" string into the orchestrator's instructions at agent-build time.
