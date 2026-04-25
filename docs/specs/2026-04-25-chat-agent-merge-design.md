# Merge GuardrailAgent into ChatAgent — Design Spec

**Date:** 2026-04-25
**Depends on:** existing `agents/guardrail_agent.py`, `orchestrator/chat_agent.py`, `skills/workflow/redact.md`, `skills/workflow/relevance_check.md`, `models/types.py` (`GuardrailVerdict`), `main.py` boundary wiring.

## Goal

Collapse two thin classes — `GuardrailAgent` (input boundary) and `ChatAgent` (output boundary + follow-up Q&A) — into a single `ChatAgent` that owns the full human-conversation surface. The merged class lives at `agents/chat_agent.py` and exposes four skill-backed methods plus one composite convenience wrapper.

## Non-goals

- **Not** changing the per-skill behavior (`redact`, `relevance_check`, `format`, `converse` keep their existing prompts and outputs).
- **Not** refactoring `data_manager_agent`'s independent redact loop. It continues to load `skills/workflow/redact.md` directly. The merged ChatAgent does not become a shared dependency for upstream agents.
- **Not** changing the firewall stack, the orchestrator, or any other agent.
- **Not** changing the public contract of `screen()` from `main.py`'s perspective beyond the type rename (`GuardrailVerdict` → `ScreenVerdict` with deprecated alias).

## §1 — Class structure

`agents/chat_agent.py` owns one class:

```python
class ChatAgent:
    """Human-conversation boundary: input screening + output formatting + follow-up Q&A.

    Holds four skills:
      - redact            — mask identifiers in inbound text
      - relevance_check   — decide whether a question is in-scope
      - format            — render FinalAnswer as reviewer-facing markdown
      - converse          — follow-up Q&A with optional helper tools
    """

    def __init__(self, llm: FirewalledModel, logger: EventLogger,
                 tools: list | None = None): ...

    # Composite (preserves today's input-boundary behavior)
    async def screen(self, question: str) -> ScreenVerdict: ...

    # Individual skills — public, callable independently
    async def redact(self, text: str) -> str: ...
    async def relevance_check(self, question: str) -> tuple[bool, str]: ...
    @staticmethod
    def format(final: FinalAnswer) -> str: ...
    async def converse(self, message: str, context: str = "") -> str: ...
```

### Files affected

- **Created:** `agents/chat_agent.py` (merged content).
- **Deleted:** `agents/guardrail_agent.py`, `orchestrator/chat_agent.py`.
- **Type rename:** `GuardrailVerdict` → `ScreenVerdict` in `models/types.py`. `GuardrailVerdict` kept as a deprecated alias for one cycle. Field set unchanged: `passed`, `reason`, `redacted_question`.
- **Skill frontmatter updated:** `redact.md` `owner` becomes `[chat_agent, data_manager]`; `relevance_check.md` `owner` becomes `[chat_agent]`. Body references to "Guardrail Agent" become "Chat Agent".
- **Tests merged:** `tests/test_agents/test_guardrail_agent.py` and `tests/test_orchestrator/test_chat_agent.py` collapse into `tests/test_agents/test_chat_agent.py`. The orchestrator test file is removed.

## §2 — Method contracts

**`screen(question: str) -> ScreenVerdict`** — Identical behavior to today's `GuardrailAgent.screen`. Internally calls `self.redact(question)` then `self.relevance_check(redacted)`, builds and returns a `ScreenVerdict`. Fail-open semantics preserved (relevance LLM blocked → `passed=True`).

**`redact(text: str) -> str`** — Returns the redacted text only. `masked_spans` is computed by the LLM but logged through the existing fallback event (renamed) rather than returned. If a caller later wants spans, add a `redact_with_spans()` sibling — YAGNI for the merge.

**`relevance_check(question: str) -> tuple[bool, str]`** — Returns `(passed, reason)`. `screen()` composes this with `redact` to build the full `ScreenVerdict`.

**`format(final: FinalAnswer) -> str`** — Static method. Body identical to today's `format_final_answer`. Renamed for brevity (class context already implies "final answer"). `format_final_answer` kept as a deprecated class-attribute alias for one cycle.

**`converse(message: str, context: str = "") -> str`** — Unchanged from today.

### Logger event renames

| Old                              | New                            |
| -------------------------------- | ------------------------------ |
| `guardrail_start`                | `chat_screen_start`            |
| `guardrail_done`                 | `chat_screen_done`             |
| `guardrail_redact_fallback`      | `chat_redact_fallback`         |
| `guardrail_relevance_fallback`   | `chat_relevance_fallback`      |

Cosmetic, but worth doing alongside the class merge so log filtering follows the new vocabulary.

## §3 — Migration & call-site updates

**`main.py`:**

```python
# Before
from agents.guardrail_agent import GuardrailAgent
from orchestrator.chat_agent import ChatAgent
...
chat_agent = ChatAgent(llm, logger, tools=helper_tools)
guardrail = GuardrailAgent(llm, logger)
verdict = await guardrail.screen(question)
return chat_agent.format_final_answer(final)

# After
from agents.chat_agent import ChatAgent
...
chat_agent = ChatAgent(llm, logger, tools=helper_tools)
verdict = await chat_agent.screen(question)
return chat_agent.format(final)
```

One `ChatAgent` instance replaces both objects.

**Tests:**

- Move + merge `tests/test_agents/test_guardrail_agent.py` (5 tests) and `tests/test_orchestrator/test_chat_agent.py` (9 tests) into `tests/test_agents/test_chat_agent.py` (14 tests).
- Add 2 new tests: one calling `chat_agent.redact(...)` directly, one calling `chat_agent.relevance_check(...)` directly. These confirm the C-hybrid public exposure works.
- Existing assertions translate mechanically: `agent.screen(...)` instead of `guardrail.screen(...)`, `ChatAgent.format(...)` instead of `ChatAgent.format_final_answer(...)`.
- Expected total in merged file: ~16 tests, all passing.

**Backward-compat aliases** (live one cycle; remove in a follow-up):

- `GuardrailVerdict = ScreenVerdict` in `models/types.py`.
- `ChatAgent.format_final_answer = ChatAgent.format` (class-attribute alias).
- No alias for `GuardrailAgent` itself — `main.py` is the only caller and is updated atomically.

**Skill files updated** (frontmatter + body wording):

- `skills/workflow/redact.md`: frontmatter `owner: [chat_agent, data_manager]`; body line "Guardrail Agent — on every reviewer-inbound question…" becomes "Chat Agent — on every reviewer-inbound question…".
- `skills/workflow/relevance_check.md`: frontmatter `owner: [chat_agent]`; body wording adjusted analogously.

**No other files touched.** The orchestrator, the firewall stack, and the other agents (base, report, general, data_manager) are unaffected.

## §4 — Testing strategy

- **Unit tests for the merged class** in `tests/test_agents/test_chat_agent.py` cover: `screen` (pass + reject + fallback), `redact` (mock LLM returns mask), `relevance_check` (mock LLM returns pass/fail), `format` (with and without `data_pull_request`, with/without flags), `converse` (with and without helper tools).
- **No new integration tests** — the existing `test_e2e/test_smoke.py` exercises the full pipeline through `main.py`'s boundaries; if `main.py`'s wiring is correct, it passes.
- **Test count target:** ≥16 tests in the merged file, full suite ≥231 (today's count) green.

## §5 — Open questions

None.
