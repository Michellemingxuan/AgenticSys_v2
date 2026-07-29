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
    # name as registered on the orchestrator). The agent_tool wrapper
    # reads this on each invocation: when a prior history exists for the
    # specialist, the sub-agent is run with that history prepended to the
    # new sub-question, so follow-up calls see what was already asked /
    # answered. After each sub-agent run finishes, the wrapper saves
    # `result.to_input_list()` back here. Reset is per-AppContext: re-running
    # the cell that constructs a fresh AppContext starts a fresh chain.
    _specialist_histories: dict[str, list] = field(default_factory=dict)
    # Per-turn structured record of specialist invocations that failed inside
    # the agent_tool wrapper (timeouts, SDK exceptions, unexpected errors).
    # Each entry: {specialist, error_type, error_message, sub_question}.
    # Server-side stream loop drains this to emit typed `error` SSE events and
    # to append failure flags to the FinalAnswer so the reviewer sees the
    # actual cause instead of a silent "specialist did not return".
    _specialist_errors: list[dict] = field(default_factory=list)
    # Names of domain specialists that have been called this turn. Used by
    # the agent_tool guard to block general_specialist when < 2 domain
    # specialists ran (programmatic enforcement of the HARD GATE).
    _domain_specialists_called: set = field(default_factory=set)
    # Number of dispatch ROUNDS the server has driven this turn (initial
    # dispatch = 1; at most ONE server-enforced re-dispatch = 2). Enforces
    # the design's "≤ 2 dispatch rounds per turn" cap in the plan-review
    # phased run. server.py reads/bumps this via `_dispatch_count` /
    # `_bump_dispatch_count`; a re-dispatch is refused once it reaches 2.
    _dispatch_count: int = 0
    # Per-specialist KNOWLEDGE BASE — survives across turns within a case
    # session. Keyed by specialist name; each value is a chronological list of
    # KnowledgePoint dicts (Pydantic-dumped). The list is owned by
    # `CaseSession.specialist_kb` in the server; this attribute holds the
    # SAME dict by reference, so writes the agent_tool makes here persist
    # to the next turn's AppContext automatically. None when not wired (e.g.
    # tests that don't set up a session).
    _specialist_kb: dict[str, list] | None = None
    # Distiller agent (built once at orchestrator construction, shared across
    # all specialists). The agent_tool wrapper invokes it after each
    # specialist run to extract KnowledgePoints. None disables distillation
    # (graceful: the wrapper just skips the second pass and the specialist's
    # answer still flows to the orchestrator).
    _distiller: Any = None
    # Current turn id, threaded so distilled KPs can be tagged with the
    # turn that produced them — useful for audit + chronological supersession.
    _turn_id: str | None = None
    # Fire-and-forget distiller tasks. Each agent_tool wrapper schedules
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
    # NodeTraceStore handle threaded through so agent_tool wrappers can
    # open per-specialist + per-distiller NodeTrace blocks without reaching
    # for a global. None outside an active server session.
    _node_trace_store: Any = None
    # Data catalog reference — used by the post-distillation fill step to
    # auto-inject risk_threshold values into KP numbers from structured
    # profile metadata (no LLM extraction needed).
    _catalog: Any = None
    # Episodic record window (built from qa_cache each turn) — this specialist's
    # own slice is prepended to its sub-question by agent_tool._runner.
    _episodic_records: list = field(default_factory=list)
    # ── Amem integration (set in conductor._assemble_input) ──────────────
    _amem: Any = None                    # AmemManager or NullAmemManager
    _amem_cfg: Any = None                # memory.AmemConfig
    _case_id: str | None = None          # sess.case_id, for scope building
    _session_id: str | None = None       # sess.session_id, for Amem metadata
    # Per-specialist data collected DURING the turn for the batched end-of-turn
    # durable write (see agent_tool._runner + conductor._persist_to_amem).
    # Keyed by specialist name; each value: {"sub_question", "findings",
    # "tool_calls"}. report_agent is excluded (not a data specialist).
    _specialist_turn_records: dict = field(default_factory=dict)
    # Specialists whose run STILL rested on a failed tool call after the
    # grounding retry (see agent_factories/agent_tools/grounding.py). Their
    # answer is returned to the orchestrator with a degraded banner, but is
    # kept OUT of every cross-turn channel — distiller/KB, Amem, the qa_cache
    # tool_calls that feed episodic, and the intra-turn dedup cache — so a
    # wrong answer this turn cannot ground the next one.
    # Keyed by specialist name; each value: list of the grounding error dicts.
    _degraded_specialists: dict = field(default_factory=dict)
    # Measured wall-clock per specialist run, `{name: [ms, ...]}` in completion
    # order, published by agent_tool. The SSE layer prefers these over
    # stream-event timing: the SDK gathers PARALLEL tool calls and only queues
    # their output items after the slowest finishes, so stream-derived durations
    # are identical for every sibling and report the batch, not the specialist.
    _specialist_run_ms: dict = field(default_factory=dict)
