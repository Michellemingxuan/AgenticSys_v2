"""Guardrail Agent — input-side boundary.

Sits between the reviewer and the rest of the system. Every question routes
through here before the Orchestrator runs. The agent chains two inline
skills:

  1. `workflow/redact.md`          — mask identifiers + injection tokens
  2. `workflow/relevance_check.md` — decide whether the question is
                                     in-scope for case review

A `GuardrailVerdict(passed=False)` short-circuits the pipeline with a
reviewer-facing `reason`. A pass yields the redacted question for the
Orchestrator to run on.
"""

from __future__ import annotations

from pathlib import Path

from llm.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import GuardrailVerdict
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


class GuardrailAgent:
    """Screens reviewer questions before the Orchestrator touches them."""

    def __init__(self, llm: FirewalledModel, logger: EventLogger):
        self.llm = llm
        self.logger = logger
        self._redact_prompt = _load_skill(_WORKFLOW_DIR / "redact.md").body
        self._relevance_prompt = _load_skill(_WORKFLOW_DIR / "relevance_check.md").body

    async def screen(self, question: str) -> GuardrailVerdict:
        """Redact identifiers, then decide in-scope vs reject.

        If the redact step is blocked, falls through with the raw question
        so relevance-check still gets a chance. If the relevance step is
        blocked, defaults to `passed=True` (fail-open on guardrail blocks
        so a reviewer isn't silently stonewalled by a firewall hiccup).
        """
        self.logger.log("guardrail_start", {"question_len": len(question)})

        # Step 1 — redact.
        redact_result = await self.llm.ainvoke(
            system_prompt=self._redact_prompt,
            user_message=(
                f"Text to redact:\n\n{question}\n\n"
                "Return JSON with redacted + masked_spans."
            ),
        )

        if redact_result.status == "blocked" or redact_result.data is None:
            self.logger.log("guardrail_redact_fallback", {"reason": "blocked"})
            redacted = question
        else:
            redacted = str(redact_result.data.get("redacted", question)) or question

        # Step 2 — relevance check on the redacted question.
        relevance_result = await self.llm.ainvoke(
            system_prompt=self._relevance_prompt,
            user_message=(
                f"Reviewer question: {redacted}\n\n"
                "Decide whether this is in-scope for case review. Return JSON "
                "with passed + reason."
            ),
        )

        if relevance_result.status == "blocked" or relevance_result.data is None:
            self.logger.log(
                "guardrail_relevance_fallback",
                {"reason": "blocked — fail-open"},
            )
            return GuardrailVerdict(passed=True, redacted_question=redacted)

        data = relevance_result.data
        passed = bool(data.get("passed", True))
        reason = str(data.get("reason", "")).strip()

        verdict = GuardrailVerdict(
            passed=passed,
            reason=reason if not passed else "",
            redacted_question=redacted,
        )
        self.logger.log("guardrail_done", {"passed": passed, "reason": verdict.reason})
        return verdict
