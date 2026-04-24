"""Chat agent — formats the final answer and handles reviewer conversation.

Two public methods:
  - `format_final_answer(FinalAnswer) -> str` — reviewer-facing markdown.
  - `converse(user_message, context)` — follow-up Q&A with optional helper
    tools bound via `tools=` on the wrapped LLM.
"""

from __future__ import annotations

from llm.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import FinalAnswer


CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant for a credit risk reviewer. "
    "Answer follow-up questions about the analysis clearly and concisely. "
    "If you reference specific data or findings, cite the source specialist. "
    "Stay within the scope of the analysis provided in the context."
)


class ChatAgent:
    """Formats final answers and handles follow-up conversation."""

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

    @staticmethod
    def format_final_answer(final: FinalAnswer) -> str:
        """Render a FinalAnswer as reviewer-facing markdown.

        Sections: Answer, Flags (if any), Provenance (report coverage + files
        consulted + specialists consulted), Timeline (per-stage duration).
        Staticmethod because formatting doesn't depend on the agent's LLM
        or logger.
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
        if final.timeline:
            parts.append("\n## Timeline")
            for entry in final.timeline:
                parts.append(
                    f"- **{entry['stage']}**: {entry['duration_ms']} ms"
                )
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
