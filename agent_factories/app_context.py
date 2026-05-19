"""Per-request context object threaded through Runner.run for tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class AppContext:
    gateway: Any
    case_folder: Path
    logger: Any
    # Per-specialist conversation history, keyed by tool name (== specialist
    # name as registered on the orchestrator). The redacting_tool wrapper
    # reads this on each invocation: when a prior history exists for the
    # specialist, the sub-agent is run with that history prepended to the
    # new sub-question, so follow-up calls see what was already asked /
    # answered. After each sub-agent run finishes, the wrapper saves
    # `result.to_input_list()` back here. Reset is per-AppContext: re-running
    # the cell that constructs a fresh AppContext starts a fresh chain.
    _specialist_histories: dict[str, list] = field(default_factory=dict)
    # Per-turn structured record of specialist invocations that failed inside
    # the redacting_tool wrapper (timeouts, SDK exceptions, unexpected errors).
    # Each entry: {specialist, error_type, error_message, sub_question}.
    # Server-side stream loop drains this to emit typed `error` SSE events and
    # to append failure flags to the FinalAnswer so the reviewer sees the
    # actual cause instead of a silent "specialist did not return".
    _specialist_errors: list[dict] = field(default_factory=list)
    # Per-specialist KNOWLEDGE BASE — survives across turns within a case
    # session. Keyed by specialist name; each value is a chronological list of
    # KnowledgePoint dicts (Pydantic-dumped). The list is owned by
    # `CaseSession.specialist_kb` in the server; this attribute holds the
    # SAME dict by reference, so writes the redacting_tool makes here persist
    # to the next turn's AppContext automatically. None when not wired (e.g.
    # tests that don't set up a session).
    _specialist_kb: dict[str, list] | None = None
    # Distiller agent (built once at orchestrator construction, shared across
    # all specialists). The redacting_tool wrapper invokes it after each
    # specialist run to extract KnowledgePoints. None disables distillation
    # (graceful: the wrapper just skips the second pass and the specialist's
    # answer still flows to the orchestrator).
    _distiller: Any = None
    # Current turn id, threaded so distilled KPs can be tagged with the
    # turn that produced them — useful for audit + chronological supersession.
    _turn_id: str | None = None
    # Fire-and-forget distiller tasks. Each redacting_tool wrapper schedules
    # distillation as an asyncio.Task here BEFORE returning the specialist's
    # payload to the orchestrator — so the orchestrator gets the answer
    # without waiting on the distiller round-trip. Server.py awaits all
    # pending tasks at end of turn so the KB is fully populated before the
    # NEXT turn starts (and its KB-warmth digest reflects this turn's KPs).
    _pending_distillers: list = field(default_factory=list)
    # Server-side SSE-emit hook. When wired by `server.py` at turn start,
    # tools running inside `Runner.run` can publish typed events out to the
    # frontend WITHOUT going through the orchestrator's run loop — e.g.
    # `make_chart` calls this to fire a `chart_pending` event the instant
    # a specialist starts plotting, so the UI can show a "working on the
    # plots" placeholder long before the actual `chart` event (which only
    # fires at end-of-turn after distillation drains). None outside an
    # active session (tests, notebooks); tools must guard the call.
    # Signature: `_emit_event(event_name: str, payload: dict) -> None`.
    _emit_event: Callable[[str, dict], None] | None = None
