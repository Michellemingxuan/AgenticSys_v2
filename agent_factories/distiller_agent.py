"""Distiller Agent — second-pass extractor of reusable knowledge points.

After each specialist run, the redacting_tool wrapper invokes this agent
on the SpecialistOutput to pull out atomic, quantitative claims that future
turns might revisit. The points land in
``CaseSession.specialist_kb[<specialist_name>]`` and are prepended (as a
digest) to the specialist's sub-question on subsequent calls — so the
specialist sees what it already knows and can answer follow-ups without
re-running expensive `summarize_trend` / `aggregate_column` queries.

Why a second pass instead of asking the specialist to emit knowledge_points
inline:
- Distillation is a different cognitive task than analysis. Asking the
  specialist to do both reliably bloats its prompt and degrades both.
- A separate, narrowly-scoped agent with a strict output schema is more
  faithful (less paraphrasing) and cheaper to iterate on.
- Failures in distillation (timeout, malformed output) degrade gracefully
  to "no KB update this turn"; the specialist's answer is unaffected.
"""
from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema, ModelSettings

from models.types import DistillerOutput
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"

_DISTILLER_PROMPT = (
    "You are a knowledge-point distiller for a credit-risk case-review system.\n\n"
    + _load_skill(_WORKFLOW_DIR / "distillation.md").body
)


def build_distiller_agent(model) -> Agent:
    """Construct the distiller. Stateless — one instance is shared across
    all specialists' wrappers in a session.

    No tools and no ``tool_choice``: the distiller emits structured output
    directly. OpenAI's API rejects ``tool_choice`` when ``tools`` is empty
    ("'tool_choice' is only allowed when 'tools' are specified"), so we
    leave both unset. We disable strict_json_schema because ``numbers`` and
    ``viz`` are open-ended dicts (the specialist's actual data shape
    varies) — strict mode rejects free-form dict fields.
    """
    return Agent(
        name="distiller",
        instructions=_DISTILLER_PROMPT,
        tools=[],
        output_type=AgentOutputSchema(DistillerOutput, strict_json_schema=False),
        model=model,
        model_settings=ModelSettings(max_tokens=1200),
    )
