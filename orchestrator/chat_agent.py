"""Chat agent — formats final output and handles reviewer conversation."""

from __future__ import annotations

from gateway.firewall_stack import FirewallStack
from log.event_logger import EventLogger
from models.types import FinalOutput


CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant for a credit risk reviewer. "
    "Answer follow-up questions about the analysis clearly and concisely. "
    "If you reference specific data or findings, cite the source specialist. "
    "Stay within the scope of the analysis provided in the context."
)


class ChatAgent:
    """Formats analysis output and handles follow-up conversation."""

    def __init__(self, firewall: FirewallStack, logger: EventLogger):
        self.firewall = firewall
        self.logger = logger

    def format_for_reviewer(self, final_output: FinalOutput) -> str:
        parts: list[str] = []

        # Main answer
        parts.append(final_output.answer)

        # Open conflicts requiring attention
        if final_output.open_conflicts:
            parts.append("\n--- REQUIRES YOUR ATTENTION ---")
            for conflict in final_output.open_conflicts:
                parts.append(
                    f"  Conflict between {conflict.pair[0]} and {conflict.pair[1]}: "
                    f"{conflict.contradiction}"
                )
                parts.append(f"    Reason unresolved: {conflict.reason_unresolved}")

        # Data gap summary (concise high-level) — preferred when available
        if final_output.data_gap_summary:
            parts.append(f"\n--- DATA GAP SUMMARY ---\n{final_output.data_gap_summary}")

        # Only show detailed list for gaps flagged as SIGNALS (avoid noise)
        signal_gaps = [g for g in final_output.data_gaps if g.is_signal]
        if signal_gaps:
            parts.append("\n--- DATA GAPS (flagged as signals) ---")
            for gap in signal_gaps:
                parts.append(f"  {gap.specialist}: {gap.missing_data}")
                if gap.absence_interpretation:
                    parts.append(f"    Interpretation: {gap.absence_interpretation}")

        # Blocked / incomplete analyses
        if final_output.blocked_steps:
            parts.append("\n--- INCOMPLETE ANALYSES ---")
            for step in final_output.blocked_steps:
                parts.append(f"  {step.specialist}: {step.error}")

        # Specialists consulted
        if final_output.specialists_consulted:
            parts.append(
                f"\nSpecialists consulted: {', '.join(final_output.specialists_consulted)}"
            )

        return "\n".join(parts)

    def converse(self, user_message: str, context: str = "") -> str:
        full_context = context
        user_msg = user_message

        if full_context:
            system = f"{CHAT_SYSTEM_PROMPT}\n\nAnalysis context:\n{full_context}"
        else:
            system = CHAT_SYSTEM_PROMPT

        result = self.firewall.call(
            system_prompt=system,
            user_message=user_msg,
        )

        if result.status == "blocked":
            return (
                "I'm unable to process that request due to content restrictions. "
                "Could you please rephrase your question?"
            )

        data = result.data or {}
        return data.get("response", data.get("answer", str(data)))
