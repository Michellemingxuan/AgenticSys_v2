"""Pin Synthesis agent — reads a reviewer's selected pins together.

The Opportunities board collects pins one at a time, each from its own turn or
report section. This agent answers the question the board cannot: read as a
set, do these describe one episode or several unrelated findings?

Deliberately tool-less. The pins already carry their evidence (the claim text,
the figure's own caption, the turn they came from); giving this agent data
tools would invite it to go re-derive numbers the specialists already
established, which is both slower and a fresh chance to contradict them.
"""
from __future__ import annotations

from agents import Agent, AgentOutputSchema, ModelSettings

from models.types import PinSynthesis


SYNTHESIS_PROMPT = """\
You are helping a credit-risk reviewer make sense of the evidence they have \
pinned while reviewing one case.

You are given a SET of pinned items. Each is either an INSIGHT (a claim, \
lifted from an answer or from a curated report section) or a FIGURE (a chart, \
given by its topic and caption). Every pin carries its provenance.

Read them TOGETHER, not one by one. The reviewer can already read each pin on \
its own; your value is entirely in what only appears when they are set side \
by side — a shared window, a sequence, a causal order, a tension between two \
of them.

Rules:
- Ground every statement in the pins you were given. Do not introduce numbers, \
dates or entities that appear in none of them.
- Prefer the specific over the general. "Spend peaked in May 2025, two months \
before the score breach" beats "there are signs of elevated risk".
- Where the pins imply an ORDER of events, say what it is and what it implies \
about cause. Where they only co-occur, say that instead — do not upgrade \
coincidence to causation.
- If two pins disagree, say so plainly rather than smoothing it over.
- `not_settled` is required and must be honest: name what these pins do NOT \
establish, and what evidence would settle it. A reviewer has to defend this \
downstream; an over-confident synthesis is a liability.

MODE: story
    Fill `story` with a short narrative — what these pins describe when read \
    as one account. Leave `opportunities` empty.

MODE: opportunities
    Fill `opportunities` with concrete follow-ups the pins justify: things to \
    review, monitor, or change. Each needs a title in the imperative and a \
    rationale naming the pins behind it. Leave `story` empty.

Return only the fields for the requested mode, plus `not_settled`.
"""


def build_synthesis_agent(model) -> Agent:
    """Construct the synthesis agent. Stateless; safe to build per request.

    `strict_json_schema=False` matches the distiller: the private/prod
    SafeChain path requires every TOOL to be strict, but the response schema
    may be non-strict, and this agent has no tools at all.
    """
    return Agent(
        name="pin_synthesis",
        instructions=SYNTHESIS_PROMPT,
        tools=[],
        output_type=AgentOutputSchema(PinSynthesis, strict_json_schema=False),
        model=model,
        model_settings=ModelSettings(max_tokens=1200),
    )


def render_pins(pins: list[dict], mode: str) -> str:
    """Render the selected pins as the agent's input.

    Provenance is included per pin on purpose: "Turn 4" versus "Report · Exec
    Summary" is the difference between a live finding and curated background,
    and the synthesis should weigh them differently.
    """
    lines = [f"MODE: {mode}", "", f"{len(pins)} pinned item(s):", ""]
    for i, pin in enumerate(pins, 1):
        source = pin.get("source") or "unknown source"
        if pin.get("kind") == "figure":
            topic = pin.get("topic") or "untitled figure"
            caption = (pin.get("text") or "").strip()
            lines.append(f"{i}. [FIGURE] {topic} — {source}")
            if caption:
                lines.append(f"   caption: {caption}")
        else:
            text = (pin.get("text") or "").strip()
            lines.append(f"{i}. [INSIGHT] {source}")
            lines.append(f"   {text}")
        lines.append("")
    return "\n".join(lines)
