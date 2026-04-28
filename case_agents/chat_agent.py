"""Chat Agent — human-conversation boundary.

Owns the input boundary (redact + relevance_check), output formatting, and
follow-up Q&A. Replaces the previous split between `GuardrailAgent` (input)
and the orchestrator-side `ChatAgent` (output).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        llm: Any,
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
