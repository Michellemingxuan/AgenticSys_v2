"""Chat Agent — human-conversation boundary.

Owns the input boundary (redact + relevance_check), output formatting, and
follow-up Q&A. Replaces the previous split between `GuardrailAgent` (input)
and the orchestrator-side `ChatAgent` (output).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from logger.event_logger import EventLogger
from models.types import ClarifyResult, FinalAnswer, ScreenVerdict
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"


CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant for a credit risk reviewer. "
    "Answer follow-up questions about the analysis clearly and concisely. "
    "If you reference specific data or findings, cite the source specialist. "
    "Stay within the scope of the analysis provided in the context."
)


def _is_trivially_safe_question(text: str) -> bool:
    """True when the text plainly carries no identifiers we'd need to
    redact. Used to skip the redact LLM round-trip on short out-of-scope
    questions ("what to eat", "hi", etc.) — the redact step is masking
    case IDs / SSNs / emails which by definition can't be present here.

    Rules (conservative; favors running redact when in doubt):
      - Length < 80 chars (long inputs are more likely to embed something).
      - No digit run of 3+ characters (case IDs are 12-digit strings; this
        also catches account numbers, phone digits, etc.).
      - No '@' (emails).
    """
    if not text or len(text) >= 80:
        return False
    if "@" in text:
        return False
    run = 0
    for ch in text:
        if ch.isdigit():
            run += 1
            if run >= 3:
                return False
        else:
            run = 0
    return True


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
        pillar_config: dict | None = None,
    ):
        self.llm = llm
        self.logger = logger
        self.tools = tools
        # `concept_glossary` is the pillar's domain-vocabulary block (e.g.
        # "'CPS' ≈ consumer, 'SBS' ≈ commercial" for credit-risk). Surfacing
        # it inside relevance_check lets the LLM recognise that two questions
        # using different but synonymous terms ("how many SBS cards" vs
        # "how many commercial cards") refer to the same subject — without
        # which the near-duplicate detector reports them as distinct.
        glossary = ""
        if isinstance(pillar_config, dict):
            glossary = str(pillar_config.get("concept_glossary") or "").strip()
        self._concept_glossary = glossary
        self._redact_prompt = _load_skill(_WORKFLOW_DIR / "redact.md").body
        self._relevance_prompt = _load_skill(_WORKFLOW_DIR / "relevance_check.md").body
        self._clarify_prompt = _load_skill(_WORKFLOW_DIR / "clarify_intent.md").body

    # ── Input boundary ────────────────────────────────────────────────────

    async def screen(
        self,
        question: str,
        prior_questions: list[str] | None = None,
    ) -> ScreenVerdict:
        """Redact identifiers, then decide in-scope vs reject. Also detects
        whether ``question`` is a near-duplicate of any entry in
        ``prior_questions`` (earlier reviewer questions in the same session,
        most recent last) so the server can replay a cached answer.

        If the redact step is blocked, falls through with the raw question
        so relevance-check still gets a chance. If the relevance step is
        blocked, defaults to `passed=True` (fail-open on guardrail blocks
        so a reviewer isn't silently stonewalled by a firewall hiccup).

        Fast path: short, plainly-non-sensitive questions (< 80 chars, no
        digits, no '@') skip the redact LLM call entirely — there's nothing
        to mask and the round-trip just adds 1-2s of latency for trivial
        inputs like "what to eat" or "hi". Anything that LOOKS like it
        could carry identifiers (digit sequences = case IDs / SSNs / phone
        numbers, '@' = emails) still goes through redact.
        """
        self.logger.log(
            "chat_screen_start",
            {"question_len": len(question),
             "n_prior_questions": len(prior_questions or [])},
        )

        if _is_trivially_safe_question(question):
            self.logger.log("chat_redact_skipped",
                            {"reason": "trivial_no_pii", "question_len": len(question)})
            redacted = question
        else:
            redacted = await self.redact(question)
        passed, reason, near_dup, near_dup_reason = await self.relevance_check(
            redacted, prior_questions=prior_questions or []
        )

        verdict = ScreenVerdict(
            passed=passed,
            reason=reason if not passed else "",
            redacted_question=redacted,
            near_duplicate_of=near_dup if passed else "",
            near_duplicate_reason=near_dup_reason if passed else "",
        )
        self.logger.log(
            "chat_screen_done",
            {"passed": passed, "reason": verdict.reason,
             "near_duplicate_of": verdict.near_duplicate_of,
             "near_duplicate_reason": verdict.near_duplicate_reason},
        )
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
            json_mode=True,
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("chat_redact_fallback", {"reason": "blocked"})
            return text

        return str(result.data.get("redacted", text)) or text

    async def relevance_check(
        self,
        question: str,
        prior_questions: list[str] | None = None,
    ) -> tuple[bool, str, str, str]:
        """Decide whether the question is in-scope AND whether it's a
        near-duplicate of any prior question. Returns
        ``(passed, reason, near_duplicate_of, near_duplicate_reason)``.

        Fail-open: if the LLM is blocked, returns
        ``(True, "", "", "")`` so a firewall hiccup doesn't stonewall a
        reviewer or accidentally replay a stale cached answer.
        """
        prior = prior_questions or []
        prior_block = (
            "\nPrior reviewer questions in this session (most recent last):\n"
            + "\n".join(f"  - {q}" for q in prior)
            if prior else
            "\n(No prior reviewer questions yet — this is the first turn.)\n"
        )
        glossary_block = (
            "\nDomain vocabulary (treat these synonyms as the same subject "
            "when comparing questions for near-duplicates):\n"
            + self._concept_glossary
            + "\n"
            if self._concept_glossary else ""
        )
        result = await self.llm.ainvoke(
            system_prompt=self._relevance_prompt,
            user_message=(
                f"Reviewer question: {question}\n"
                f"{prior_block}"
                f"{glossary_block}\n"
                "Decide whether this is in-scope for case review AND, if "
                "in-scope, whether it is a near-duplicate of one of the "
                "prior questions (matched on subject + time-range + scope, "
                "applying the domain-vocabulary synonyms above when judging "
                "subject equivalence). Return JSON with passed + reason + "
                "near_duplicate_of + near_duplicate_reason."
            ),
            json_mode=True,
        )

        if result.status == "blocked" or result.data is None:
            self.logger.log("chat_relevance_fallback", {"reason": "blocked — fail-open"})
            return True, "", "", ""

        data = result.data
        # If JSON parse failed in the shim, the data dict carries
        # `_json_parse_error: True` instead of the expected keys. In that case
        # fail-open with a clear log so we don't silently reject everything.
        if data.get("_json_parse_error"):
            self.logger.log("chat_relevance_fallback",
                            {"reason": "json_parse_error — fail-open",
                             "raw": data.get("raw", "")[:200]})
            return True, "", "", ""
        passed = bool(data.get("passed", True))
        reason = str(data.get("reason", "")).strip()
        near_dup = str(data.get("near_duplicate_of", "")).strip()
        near_dup_reason = str(data.get("near_duplicate_reason", "")).strip()
        # Defensive: only honour near_dup when it actually matches one of the
        # prior questions verbatim (the LLM can hallucinate a paraphrase).
        if near_dup and near_dup not in prior:
            self.logger.log("chat_relevance_near_dup_dropped",
                            {"reason": "near_duplicate_of not in prior_questions",
                             "claimed": near_dup[:120]})
            near_dup = ""
            near_dup_reason = ""
        return passed, reason, near_dup, near_dup_reason

    async def clarify_intent(self, question: str) -> ClarifyResult:
        """Decide whether an in-scope question's intent is clear, or whether
        the reviewer should pick between candidate interpretations first.

        Returns a ``ClarifyResult``:
          - ``needs_clarification=False`` → dispatch the question as-is to
            the orchestrator.
          - ``needs_clarification=True`` → present ``options`` to the reviewer,
            wait for them to pick, then dispatch the chosen one.

        Fail-open: on a blocked LLM call, returns ``needs_clarification=False``
        so the pipeline still progresses.
        """
        self.logger.log("chat_clarify_start", {"question_len": len(question)})
        result = await self.llm.ainvoke(
            system_prompt=self._clarify_prompt,
            user_message=(
                f"Reviewer question: {question}\n\n"
                "Decide whether clarification is needed. Return JSON with "
                "needs_clarification + options + reason per the schema."
            ),
            json_mode=True,
        )
        if result.status == "blocked" or result.data is None:
            self.logger.log("chat_clarify_fallback", {"reason": "blocked — fail-open"})
            return ClarifyResult(needs_clarification=False, options=[], reason="")

        data = result.data
        needs = bool(data.get("needs_clarification", False))
        options = list(data.get("options") or [])
        reason = str(data.get("reason", "")).strip()
        # Defensive: if needs=True but no options, treat as no-clarification.
        if needs and not options:
            needs = False
        # Cap at 4 options per the skill spec.
        if len(options) > 4:
            options = options[:4]
        verdict = ClarifyResult(needs_clarification=needs, options=options, reason=reason)
        self.logger.log("chat_clarify_done", {
            "needs_clarification": verdict.needs_clarification,
            "n_options": len(verdict.options),
        })
        return verdict

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
        # Provenance is only populated under the legacy two-branch (Reports +
        # Team) path or the beta trace-extraction fallback. Under A1 the
        # orchestrator emits answer + flags directly without nested drafts.
        if final.report_draft is not None or final.team_draft is not None:
            prov_lines = ["\n## Provenance"]
            if final.report_draft is not None:
                prov_lines.append(f"- Report coverage: {final.report_draft.coverage}")
                prov_lines.append(
                    f"- Files consulted: {final.report_draft.files_consulted or '(none)'}"
                )
            if final.team_draft is not None:
                prov_lines.append(
                    f"- Specialists consulted: {final.team_draft.specialists_consulted or '(none)'}"
                )
            parts.append("\n".join(prov_lines))

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
