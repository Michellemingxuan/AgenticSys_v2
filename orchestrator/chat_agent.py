"""Chat agent — formats final output and handles reviewer conversation."""

from __future__ import annotations

import re

from gateway.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import FinalOutput, SpecialistOutput


_BULLET_PREFIX = re.compile(r"^\s*[-•◦*]\s+")


def _clean_findings(text: str) -> str:
    """Collapse the findings string into a single clean line.

    By prompt contract (chat.specialist) findings is one sentence starting
    with "Overall: ". In practice the model sometimes emits stray bullets
    or extra lines — strip bullet markers and join into one line so the
    display stays tight. All supporting points belong in ``evidence``.
    """
    if not text:
        return ""
    parts: list[str] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts.append(_BULLET_PREFIX.sub("", ln).strip())
    return " ".join(parts).strip()


CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant for a credit risk reviewer. "
    "Answer follow-up questions about the analysis clearly and concisely. "
    "If you reference specific data or findings, cite the source specialist. "
    "Stay within the scope of the analysis provided in the context."
)


class ChatAgent:
    """Formats analysis output and handles follow-up conversation."""

    def __init__(
        self,
        llm: FirewalledModel,
        logger: EventLogger,
        tools: list | None = None,
    ):
        self.llm = llm
        self.logger = logger
        # Optional list of callables (helper skills in tool mode) to bind on
        # every converse() call. None keeps the legacy no-tools behavior.
        self.tools = tools

    def format_for_reviewer(
        self,
        final_output: FinalOutput,
        specialist_outputs: dict[str, SpecialistOutput] | None = None,
        selected: list[str] | None = None,
        warm: list[dict] | None = None,
    ) -> str:
        parts: list[str] = []

        # ── Answer ────────────────────────────────────────────────
        parts.append("## Answer\n")
        parts.append(final_output.answer)

        # ── Specialists consulted / team context ──────────────────
        if selected or warm or final_output.specialists_consulted:
            parts.append("\n## Specialists consulted")
            if selected:
                parts.append(f"- Selected for this question: {', '.join(selected)}")
            if warm:
                warm_names = [
                    f"{a.get('domain')} ({a.get('questions_answered', 0)} prior)"
                    for a in warm
                ]
                parts.append(f"- Warm in session: {', '.join(warm_names)}")
            if final_output.specialists_consulted and not selected:
                parts.append(f"- Consulted: {', '.join(final_output.specialists_consulted)}")

        # ── Sub-questions (team plan) ─────────────────────────────
        if final_output.sub_questions:
            parts.append("\n## Sub-questions assigned to specialists")
            for sub in final_output.sub_questions:
                parts.append(f"- **{sub.specialist}**: {sub.sub_question}")

        # ── Per-specialist findings ───────────────────────────────
        if specialist_outputs:
            parts.append("\n## Per-specialist findings")
            for domain, output in specialist_outputs.items():
                parts.append(f"\n**{domain}**")
                # Findings is a single "Overall: ..." sentence by contract.
                # All supporting reasons live in evidence (orthogonal bullets).
                finding = _clean_findings(output.findings)
                if finding:
                    parts.append(f"- **Findings**: {finding}")
                if output.evidence:
                    parts.append("- **Evidence**:")
                    for ev in output.evidence:
                        parts.append(f"  - {ev}")
                if output.data_gaps:
                    parts.append("- **Data gaps**:")
                    for gap in output.data_gaps:
                        parts.append(f"  - {gap}")

        # ── Cross-domain insights ─────────────────────────────────
        if final_output.cross_domain_insights:
            parts.append("\n## Cross-domain insights")
            for insight in final_output.cross_domain_insights:
                parts.append(f"- {insight}")

        # ── Resolved contradictions ───────────────────────────────
        if final_output.resolved_contradictions:
            parts.append("\n## Resolved contradictions")
            for res in final_output.resolved_contradictions:
                parts.append(f"- **{res.pair[0]} vs {res.pair[1]}**: {res.contradiction}")
                parts.append(f"  - Question raised: {res.question_raised}")
                parts.append(f"  - Conclusion: {res.conclusion}")

        # ── Data requests made during review ──────────────────────
        if final_output.data_requests_made:
            parts.append("\n## Data requests made during review")
            for req in final_output.data_requests_made:
                desc = req.get("description") or req.get("request") or str(req)
                target = req.get("specialist") or req.get("target")
                prefix = f"{target}: " if target else ""
                parts.append(f"- {prefix}{desc}")

        # ── Open conflicts requiring attention ────────────────────
        if final_output.open_conflicts:
            parts.append("\n## Open conflicts — requires attention")
            for conflict in final_output.open_conflicts:
                parts.append(
                    f"- **{conflict.pair[0]} vs {conflict.pair[1]}**: {conflict.contradiction}"
                )
                parts.append(f"  - Reason unresolved: {conflict.reason_unresolved}")

        # ── Data gap summary + signal gaps ────────────────────────
        if final_output.data_gap_summary:
            parts.append("\n## Data gap summary")
            parts.append(f"- {final_output.data_gap_summary}")

        signal_gaps = [g for g in final_output.data_gaps if g.is_signal]
        if signal_gaps:
            parts.append("\n## Data gaps flagged as signals")
            for gap in signal_gaps:
                parts.append(f"- **{gap.specialist}**: {gap.missing_data}")
                if gap.absence_interpretation:
                    parts.append(f"  - Interpretation: {gap.absence_interpretation}")

        # ── Incomplete analyses ───────────────────────────────────
        if final_output.blocked_steps:
            parts.append("\n## Incomplete analyses")
            for step in final_output.blocked_steps:
                parts.append(f"- **{step.specialist}**: {step.error}")

        return "\n".join(parts)

    async def converse(self, user_message: str, context: str = "") -> str:
        full_context = context
        user_msg = user_message

        if full_context:
            system = f"{CHAT_SYSTEM_PROMPT}\n\nAnalysis context:\n{full_context}"
        else:
            system = CHAT_SYSTEM_PROMPT

        result = await self.llm.ainvoke(
            system_prompt=system,
            user_message=user_msg,
            tools=self.tools,
        )

        if result.status == "blocked":
            return (
                "I'm unable to process that request due to content restrictions. "
                "Could you please rephrase your question?"
            )

        data = result.data or {}
        return data.get("response", data.get("answer", str(data)))
