# Merge GuardrailAgent into ChatAgent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace `GuardrailAgent` + `ChatAgent` (two thin classes) with a single `agents/chat_agent.py` that owns the four skill-backed methods (`redact`, `relevance_check`, `format`, `converse`) plus a `screen()` convenience wrapper.

**Architecture:** Three commits, each independently green. Add the new type with alias first; create the merged class file; then atomically swap callers, update skill metadata, and delete the old files.

**Tech Stack:** Python 3.11, pydantic, pytest, existing modules.

**Spec reference:** `docs/specs/2026-04-25-chat-agent-merge-design.md`.

---

## Task 1: Add `ScreenVerdict` to `models/types.py` (non-breaking)

**Files:** Modify `models/types.py`.

- [ ] **Step 1: Rename `GuardrailVerdict` → `ScreenVerdict`, keep alias**

In `models/types.py`, replace the `GuardrailVerdict` class definition with `ScreenVerdict` (same fields), and add an alias line:

```python
class ScreenVerdict(BaseModel):
    """Output of ChatAgent.screen() — whether a reviewer's question is in-scope
    for case review, and what the redacted version of the question looks like.

    `passed=False` short-circuits the Orchestrator; `reason` is the
    reviewer-facing message explaining why.
    """

    passed: bool
    reason: str = ""
    redacted_question: str


# Backwards-compat alias — `GuardrailVerdict` was the old name when input
# screening lived in a separate `GuardrailAgent`. Removed in a follow-up
# after external consumers migrate.
GuardrailVerdict = ScreenVerdict
```

- [ ] **Step 2: Run tests**

```bash
pytest
```

Expected: all green (alias keeps existing imports working).

- [ ] **Step 3: Commit**

```bash
git add models/types.py
git commit -m "refactor(models): rename GuardrailVerdict to ScreenVerdict (alias kept)"
```

---

## Task 2: Create `agents/chat_agent.py` (the merged class)

**Files:** Create `agents/chat_agent.py`. (Old `agents/guardrail_agent.py` and `orchestrator/chat_agent.py` stay in place for now — they're still imported by `main.py` and tests.)

- [ ] **Step 1: Write the merged class**

Full content (paste verbatim):

```python
"""Chat Agent — human-conversation boundary.

Owns the input boundary (redact + relevance_check), output formatting, and
follow-up Q&A. Replaces the previous split between `GuardrailAgent` (input)
and the orchestrator-side `ChatAgent` (output).
"""

from __future__ import annotations

from pathlib import Path

from llm.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import FinalAnswer, ScreenVerdict
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant for a credit risk reviewer. "
    "Answer follow-up questions about the analysis clearly and concisely. "
    "If you reference specific data or findings, cite the source specialist. "
    "Stay within the scope of the analysis provided in the context."
)


class ChatAgent:
    """Human-conversation boundary: input screening + output formatting + follow-up Q&A.

    Holds four skills:
      - redact            — mask identifiers in inbound text
      - relevance_check   — decide whether a question is in-scope
      - format            — render FinalAnswer as reviewer-facing markdown
      - converse          — follow-up Q&A with optional helper tools
    """

    def __init__(
        self,
        llm: FirewalledModel,
        logger: EventLogger,
        tools: list | None = None,
    ):
        self.llm = llm
        self.logger = logger
        self.tools = tools
        self._redact_prompt = _load_skill(_WORKFLOW_DIR / "redact.md").body
        self._relevance_prompt = _load_skill(_WORKFLOW_DIR / "relevance_check.md").body

    # ── Input boundary ────────────────────────────────────────────────────

    async def screen(self, question: str) -> ScreenVerdict:
        """Redact identifiers, then decide in-scope vs reject.

        If the redact step is blocked, falls through with the raw question
        so relevance-check still gets a chance. If the relevance step is
        blocked, defaults to `passed=True` (fail-open on guardrail blocks
        so a reviewer isn't silently stonewalled by a firewall hiccup).
        """
        self.logger.log("chat_screen_start", {"question_len": len(question)})

        redacted = await self.redact(question)
        passed, reason = await self.relevance_check(redacted)

        verdict = ScreenVerdict(
            passed=passed,
            reason=reason if not passed else "",
            redacted_question=redacted,
        )
        self.logger.log("chat_screen_done", {"passed": passed, "reason": verdict.reason})
        return verdict

    async def redact(self, text: str) -> str:
        """Mask identifiers + injection tokens. Returns the redacted text.

        On firewall block, returns the input unchanged (logged) so callers
        don't have to handle a None / exception path.
        """
        result = await self.llm.ainvoke(
            system_prompt=self._redact_prompt,
            user_message=(
                f"Text to redact:\n\n{text}\n\n"
                "Return JSON with redacted + masked_spans."
            ),
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("chat_redact_fallback", {"reason": "blocked"})
            return text

        return str(result.data.get("redacted", text)) or text

    async def relevance_check(self, question: str) -> tuple[bool, str]:
        """Decide whether the question is in-scope. Returns (passed, reason).

        Fail-open: if the LLM is blocked, returns `(True, "")` so a firewall
        hiccup doesn't stonewall a reviewer.
        """
        result = await self.llm.ainvoke(
            system_prompt=self._relevance_prompt,
            user_message=(
                f"Reviewer question: {question}\n\n"
                "Decide whether this is in-scope for case review. Return JSON "
                "with passed + reason."
            ),
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("chat_relevance_fallback", {"reason": "blocked — fail-open"})
            return True, ""

        data = result.data
        passed = bool(data.get("passed", True))
        reason = str(data.get("reason", "")).strip()
        return passed, reason

    # ── Output boundary ───────────────────────────────────────────────────

    @staticmethod
    def format(final: FinalAnswer) -> str:
        """Render a FinalAnswer as reviewer-facing markdown.

        Sections: Answer, Flags (if any), Provenance, Data pull recommendation
        (if any), Timeline (per-stage duration).
        """
        parts: list[str] = ["## Answer\n", final.answer]
        if final.flags:
            parts.append("\n## Flags")
            for flag in final.flags:
                parts.append(f"- {flag}")
        parts.append(
            "\n## Provenance\n"
            f"- Report coverage: {final.report_draft.coverage}\n"
            f"- Files consulted: {final.report_draft.files_consulted or '(none)'}\n"
            f"- Specialists consulted: {final.team_draft.specialists_consulted or '(none)'}"
        )

        dpr = final.data_pull_request
        if dpr is not None and dpr.needed:
            would_pull_str = ", ".join(dpr.would_pull) if dpr.would_pull else "(nothing specific flagged)"
            parts.append(
                f"\n## Data pull recommendation (severity: {dpr.severity})\n"
                f"Reason: {dpr.reason}\n\n"
                f"Would pull: {would_pull_str}\n\n"
                f"> No live pull today — the Data Agent is not deployed yet. "
                f"This is a signal of what a future pull would target."
            )

        if final.timeline:
            parts.append("\n## Timeline")
            for entry in final.timeline:
                parts.append(
                    f"- **{entry['stage']}**: {entry['duration_ms']} ms"
                )
        return "\n".join(parts)

    # Backwards-compat alias for callers still using the old method name.
    # Removed in a follow-up after external consumers migrate.
    format_final_answer = format

    # ── Follow-up conversation ────────────────────────────────────────────

    async def converse(self, message: str, context: str = "") -> str:
        if context:
            system = f"{CHAT_SYSTEM_PROMPT}\n\nAnalysis context:\n{context}"
        else:
            system = CHAT_SYSTEM_PROMPT

        result = await self.llm.ainvoke(
            system_prompt=system,
            user_message=message,
            tools=self.tools,
        )

        if result.status == "blocked":
            return (
                "I'm unable to process that request due to content restrictions. "
                "Could you please rephrase your question?"
            )

        data = result.data or {}
        return data.get("response", data.get("answer", str(data)))
```

- [ ] **Step 2: Smoke-import the new module**

```bash
python3 -c "from agents.chat_agent import ChatAgent; print('OK')"
```

Expected: `OK`. Confirms imports resolve.

- [ ] **Step 3: Run tests**

```bash
pytest
```

Expected: all green. The new file isn't imported by anything yet, so existing tests (which import `agents.guardrail_agent.GuardrailAgent` and `orchestrator.chat_agent.ChatAgent`) continue to pass.

- [ ] **Step 4: Commit**

```bash
git add agents/chat_agent.py
git commit -m "feat(agents): merged ChatAgent owning screen/redact/relevance/format/converse"
```

---

## Task 3: Atomic switch — update callers, skills, tests; delete old files

**Files:**
- Modify: `main.py`, `skills/workflow/redact.md`, `skills/workflow/relevance_check.md`.
- Delete: `agents/guardrail_agent.py`, `orchestrator/chat_agent.py`, `tests/test_agents/test_guardrail_agent.py`, `tests/test_orchestrator/test_chat_agent.py`.
- Create: `tests/test_agents/test_chat_agent.py` (merged + augmented).

This task lands as one commit so the tree is never red.

- [ ] **Step 1: Update `main.py`**

Replace the two old imports and constructor calls:

```python
# Old imports (delete these two lines)
from agents.guardrail_agent import GuardrailAgent
from orchestrator.chat_agent import ChatAgent

# New import (single line)
from agents.chat_agent import ChatAgent
```

In `amain()`:

```python
# Old
chat_agent = ChatAgent(llm, logger, tools=helper_tools)
guardrail = GuardrailAgent(llm, logger)
...
verdict = await guardrail.screen(question)
...
return chat_agent.format_final_answer(final)

# New
chat_agent = ChatAgent(llm, logger, tools=helper_tools)
verdict = await chat_agent.screen(question)
...
return chat_agent.format(final)
```

(The `format_final_answer` alias on the class would also work, but the call site reads cleaner with `format`.)

- [ ] **Step 2: Update skill frontmatter + body wording**

`skills/workflow/redact.md`:

- Frontmatter `owner: [guardrail_agent, data_manager]` → `owner: [chat_agent, data_manager]`.
- Body: "**Guardrail Agent** — on every reviewer-inbound question…" → "**Chat Agent** — on every reviewer-inbound question…".

`skills/workflow/relevance_check.md`:

- Frontmatter `owner: [guardrail_agent]` → `owner: [chat_agent]`.
- Body: any "Guardrail" wording → "Chat Agent" if present.

- [ ] **Step 3: Create the merged test file**

Create `tests/test_agents/test_chat_agent.py`. It absorbs the existing assertions from both old test files plus 2 new tests for direct redact / relevance_check exposure. Full content:

```python
"""Tests for agents.chat_agent — merged ChatAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.chat_agent import ChatAgent
from logger.event_logger import EventLogger
from models.types import (
    DataPullRequest,
    FinalAnswer,
    LLMResult,
    ReportDraft,
    ScreenVerdict,
    TeamDraft,
)


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-chat", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


# ── screen() — composite: redact + relevance_check ─────────────────────────

async def test_screen_passes_in_scope_question(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"redacted": "redacted q", "masked_spans": []}),
        LLMResult(status="success", data={"passed": True, "reason": ""}),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("What's the bureau score for this case?")
    assert isinstance(verdict, ScreenVerdict)
    assert verdict.passed is True
    assert verdict.reason == ""
    assert verdict.redacted_question == "redacted q"


async def test_screen_rejects_off_topic(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"redacted": "what should I eat", "masked_spans": []}),
        LLMResult(status="success", data={"passed": False, "reason": "Off-topic — case review only."}),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("What should I eat for lunch?")
    assert verdict.passed is False
    assert "case review" in verdict.reason.lower()


async def test_screen_redact_blocked_falls_through(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="blocked", data=None, error="firewall hit"),
        LLMResult(status="success", data={"passed": True, "reason": ""}),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("any question")
    assert verdict.passed is True
    # Redact fall-through: redacted_question == raw input.
    assert verdict.redacted_question == "any question"


async def test_screen_relevance_blocked_fails_open(mock_llm, logger):
    """When relevance_check is blocked, default to passed=True."""
    mock_llm.ainvoke = AsyncMock(side_effect=[
        LLMResult(status="success", data={"redacted": "q", "masked_spans": []}),
        LLMResult(status="blocked", data=None, error="firewall hit"),
    ])
    agent = ChatAgent(mock_llm, logger)
    verdict = await agent.screen("anything")
    assert verdict.passed is True


# ── redact() — public ──────────────────────────────────────────────────────

async def test_redact_returns_redacted_text(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"redacted": "card ***MASKED***", "masked_spans": ["4532123456789"]},
    ))
    agent = ChatAgent(mock_llm, logger)
    result = await agent.redact("card 4532123456789")
    assert result == "card ***MASKED***"


async def test_redact_blocked_returns_input_unchanged(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="blocked", data=None, error="x",
    ))
    agent = ChatAgent(mock_llm, logger)
    result = await agent.redact("raw text")
    assert result == "raw text"


# ── relevance_check() — public ─────────────────────────────────────────────

async def test_relevance_check_returns_passed_reason_tuple(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"passed": False, "reason": "off-topic"},
    ))
    agent = ChatAgent(mock_llm, logger)
    passed, reason = await agent.relevance_check("anything")
    assert passed is False
    assert reason == "off-topic"


async def test_relevance_check_blocked_fails_open(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="blocked", data=None, error="x",
    ))
    agent = ChatAgent(mock_llm, logger)
    passed, reason = await agent.relevance_check("q")
    assert passed is True
    assert reason == ""


# ── format() — output ──────────────────────────────────────────────────────

def _final(data_pull_request=None, flags=None):
    return FinalAnswer(
        answer="test answer",
        flags=flags or [],
        report_draft=ReportDraft(coverage="partial"),
        team_draft=TeamDraft(answer="team answer", specialists_consulted=["bureau"]),
        data_pull_request=data_pull_request,
    )


def test_format_renders_basic_answer():
    final = FinalAnswer(
        answer="The credit risk is moderate.",
        flags=["team confirms report"],
        report_draft=ReportDraft(coverage="full", files_consulted=["bureau.md"]),
        team_draft=TeamDraft(answer="t", specialists_consulted=["bureau", "spend_payments"]),
    )
    formatted = ChatAgent.format(final)
    assert "credit risk is moderate" in formatted
    assert "bureau" in formatted
    assert "spend_payments" in formatted
    assert "Report coverage: full" in formatted
    assert "team confirms report" in formatted


def test_format_omits_flags_section_when_empty():
    final = _final(flags=[])
    formatted = ChatAgent.format(final)
    assert "\n## Flags" not in formatted


def test_format_without_pull_request_omits_section():
    formatted = ChatAgent.format(_final())
    assert "Data pull recommendation" not in formatted


def test_format_with_pull_request_renders_section():
    dpr = DataPullRequest(
        needed=True,
        reason="Need bureau refresh",
        would_pull=["bureau.fico_latest"],
        severity="high",
    )
    formatted = ChatAgent.format(_final(data_pull_request=dpr))
    assert "Data pull recommendation (severity: high)" in formatted
    assert "Need bureau refresh" in formatted
    assert "bureau.fico_latest" in formatted
    assert "No live pull today" in formatted


def test_format_with_needed_false_omits_section():
    dpr = DataPullRequest(needed=False, reason="ok", would_pull=[], severity="low")
    formatted = ChatAgent.format(_final(data_pull_request=dpr))
    assert "Data pull recommendation" not in formatted


def test_format_with_empty_would_pull_shows_placeholder():
    dpr = DataPullRequest(
        needed=True, reason="generic concern", would_pull=[], severity="low",
    )
    formatted = ChatAgent.format(_final(data_pull_request=dpr))
    assert "Would pull: (nothing specific flagged)" in formatted


def test_format_final_answer_alias_works():
    """Backwards-compat: the old method name still resolves to format()."""
    final = _final()
    assert ChatAgent.format_final_answer(final) == ChatAgent.format(final)


# ── converse() ─────────────────────────────────────────────────────────────

async def test_converse_returns_response(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(
        status="success",
        data={"response": "The bureau score indicates moderate risk."},
    ))
    agent = ChatAgent(mock_llm, logger)
    response = await agent.converse("What does the bureau score mean?", context="Score is 680")
    assert isinstance(response, str)
    assert len(response) > 0
    assert "bureau score" in response.lower()


async def test_converse_forwards_tools_to_llm(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(status="success", data={"response": "ok"}))

    def fake_helper(term: str) -> str:
        """Fake helper doc."""
        return term

    agent = ChatAgent(mock_llm, logger, tools=[fake_helper])
    await agent.converse("What is DTI?")
    call_kwargs = mock_llm.ainvoke.await_args.kwargs
    assert call_kwargs.get("tools") == [fake_helper]


async def test_converse_no_tools_passes_none(mock_llm, logger):
    mock_llm.ainvoke = AsyncMock(return_value=LLMResult(status="success", data={"response": "ok"}))
    agent = ChatAgent(mock_llm, logger)
    await agent.converse("Hi")
    call_kwargs = mock_llm.ainvoke.await_args.kwargs
    assert call_kwargs.get("tools") is None
```

- [ ] **Step 4: Delete old files**

```bash
git rm agents/guardrail_agent.py
git rm orchestrator/chat_agent.py
git rm tests/test_agents/test_guardrail_agent.py
git rm tests/test_orchestrator/test_chat_agent.py
```

- [ ] **Step 5: Run tests**

```bash
pytest
```

Expected: 16 tests in `tests/test_agents/test_chat_agent.py` pass; full suite green; total count ≥ 231 (today's count) holds (we lose 5 + 9 = 14 old tests but gain 16 new + the existing 217 unaffected).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: merge GuardrailAgent into ChatAgent (single human-conversation boundary)"
```

---

## Self-review (implementer sanity check)

Before declaring done:

1. `grep -rn "GuardrailAgent" --include="*.py" .` returns no matches.
2. `grep -rn "guardrail_agent" --include="*.py" .` returns no matches.
3. `grep -rn "from orchestrator.chat_agent" --include="*.py" .` returns no matches.
4. `python3 -c "from models.types import GuardrailVerdict, ScreenVerdict; assert GuardrailVerdict is ScreenVerdict"` succeeds.
5. `python3 -c "from agents.chat_agent import ChatAgent; assert ChatAgent.format is ChatAgent.format_final_answer"` succeeds.
6. `pytest` is all green with ≥ 231 tests.
7. Three commits exist on `main`, scopes matching their messages.
