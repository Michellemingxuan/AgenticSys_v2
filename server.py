"""HTTP server bridging the agentic backend to the Case Review Chat frontend.

Exposes the same REST + SSE contract as `CaseReviewChat/src/mockServer.ts` so
the React app can swap between the JS mock and this real server without code
changes.

Endpoints:
    GET  /api/cases                       — case list, split consumer/commercial
    POST /api/cases/<id>/turn             — start a new reviewer turn (returns turn_id)
    POST /api/cases/<id>/message          — alias of /turn (legacy compat)
    POST /api/cases/<id>/rewind           — drop everything after a message id
    GET  /api/cases/<id>/stream           — SSE: typed events as the run streams

Run:
    cd AgenticSys_v2
    pip install -r requirements.txt
    python server.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Load `.env` from the project root BEFORE anything reads ``os.environ``.
# Without this, vars like ``NODE_TRACE_DB``, ``LLM_BACKEND``, ``PILLAR``, …
# defined in `.env` are silently ignored when running `python server.py`
# directly. Standalone scripts (notebooks, datalayer.sync) already do
# this — server.py was the missing one. Lazy/optional import so the
# server still starts if python-dotenv isn't installed (e.g. private env).
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from flask_cors import CORS

from agents import Runner
from agents.exceptions import AgentsException, ModelBehaviorError
from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem

from agent_factories.app_context import AppContext
from agent_factories.chat_agent import ChatAgent
from agent_factories.helper_tools import build_helper_tools
from config.pillar_loader import PillarLoader
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from llm.factory import FirewalledChatShim, build_session_clients
from llm.firewall_stack import FirewallStack, redact_payload
from logger.event_logger import EventLogger
from logger.process_timer import ProcessTimer
from tools.node_trace import (
    NodeTrace, NodeTraceRunHooks, NodeTraceStore, TURN_SCOPE, TurnScope,
    _open_node, attach_io, attach_latency, attach_tag, attach_usage,
)
from main import _DATA_TABLES_DIR, _REPORTS_DIR, _resolve_data_source
from models.types import FinalAnswer
from orchestrator.orchestrator import Orchestrator
from tools.data_tools import init_tools


# ── Configuration ───────────────────────────────────────────────────────────

PILLAR = os.environ.get("PILLAR", "credit_risk")
MODEL = os.environ.get("MODEL", "gpt-4.1")
DATA_SOURCE = os.environ.get("DATA_SOURCE", "auto")
PORT = int(os.environ.get("PORT", 3001))
HOST = os.environ.get("HOST", "127.0.0.1")
# Per-session ring buffer of recently-emitted SSE events. When a client
# (re)connects to the stream — initial load, or recovery after a silent
# half-open death — these are replayed BEFORE going live, so a turn that ran
# while no subscriber was connected (the `turn_no_subscribers` case) is
# recovered instead of lost forever. ~400 events covers the last turn or two;
# replay is idempotent on the client (upsert by turn_id/call_id; messages
# deduped by id), so any overlap with live events is harmless.
_EVENT_BUFFER_MAX = int(os.environ.get("SSE_EVENT_BUFFER_MAX", "400"))

PING_INTERVAL_S = 5.0  # was 15.0 — tighter pings reduce silent SSE disconnects
# from idle-aware intermediaries (browser tab throttle, corporate proxies,
# dev-server proxies with default 60s idle-close). Real failure case: user
# came back to a session after ~1h idle, asked a new question; the EventSource
# had silently died during the idle window, so the new turn's events landed in
# a dead subscriber queue and the UI looked frozen. 5s is well within every
# common proxy timeout. Cost is trivial — 12 noop frames/min/client.
_QA_CACHE_MAX_ENTRIES = int(os.environ.get("QA_CACHE_MAX_ENTRIES", "64"))
_SSE_QUEUE_MAXSIZE = int(os.environ.get("SSE_QUEUE_MAXSIZE", "256"))

# Wall-clock budget for a complete turn (screen + orchestrator + all
# specialists + synthesis + drain). 360s covers the worst legitimate
# case: 240s specialist budget × parallel + 60s synthesis + buffer.
# Beyond this, the turn is stuck — most likely a hung LLM HTTP call
# (no SDK-level wall-clock fence on the orchestrator's own round-trips
# the way `_SPECIALIST_TIMEOUT_S` fences the specialists). The lock-
# release path in `_spawn_turn` depends on `_run_turn_streamed`
# returning; without this fence, a hang there strands the per-case
# `turn_lock` forever and every subsequent question on the same case
# blocks indefinitely on line 1569's unbounded `lock.acquire()`. Real
# failure mode: user restarts the server, asks a question, sees no
# response — the first turn's orchestrator hung (often a transient
# DNS/TLS warmup on a fresh AsyncOpenAI client), and every later
# question queued behind it.
_TURN_WALL_CLOCK_S = float(os.environ.get("TURN_WALL_CLOCK_S", "360"))

# Round-1 (team-planning) watchdog. The orchestrator's first LLM call decides
# which specialists to dispatch; it normally returns in ~3s, but under heavy
# private-env (safechain) traffic it can stall up to ~100s — well under the
# 180s per-call cap, so it isn't aborted and the user just waits. A fresh call
# (manual rewind/retry) almost always returns fast, so we bound the planning
# phase and auto-retry on stall. Generous margin over the ~3s norm; env-tunable.
_ORCH_PLAN_TIMEOUT_S = float(os.environ.get("ORCH_PLAN_TIMEOUT_S", "25"))

# Tighter fence around the screen phase specifically. Screen is one
# `redact` LLM call (often skipped) + one `relevance_check` LLM call —
# normally <5s end-to-end per the project's performance-targets memory.
# A 30s ceiling catches a hung LLM (most often on the safechain backend
# in private env, where transient HTTP-pool exhaustion strands a call
# that the 360s turn fence eventually catches but feels like forever
# from the user's POV). On screen timeout we surface a clear "screen
# took too long" error to the user instead of leaving them watching a
# silent "question check" spinner for 6 minutes. Real failure mode the
# user reported: "stuck in question check, refresh doesn't help, need
# to restart the server or clear history multiple times."
_SCREEN_TIMEOUT_S = float(os.environ.get("SCREEN_TIMEOUT_S", "30"))

# Cap the prior-questions list sent into the relevance_check skill.
# Without this, the prompt for the screen LLM grows as the qa_cache
# fills (up to 64 entries), and on the safechain backend a larger
# prompt is more likely to hit transient HTTP timeouts mid-call. 12
# most recent is plenty for near-duplicate detection on a single
# review session — older questions are unlikely to be re-asked
# verbatim and the cost-of-missing-one is small (the orchestrator
# still re-answers correctly, just without the cache shortcut).
_PRIOR_QUESTIONS_FOR_SCREEN = int(
    os.environ.get("PRIOR_QUESTIONS_FOR_SCREEN", "12")
)

# NodeTrace SQLite store — one per process, shared across sessions.
# Set NODE_TRACE_DISABLE=1 to turn the layer off entirely (escape hatch).
# Expand both ``~`` and ``$VAR`` references so .env values like
# ``NODE_TRACE_DB=$HOME/agenticsys-traces/node_traces.db`` or
# ``NODE_TRACE_DB=~/agenticsys-traces/node_traces.db`` resolve to a real
# absolute path. Without this, python-dotenv hands us the literal string
# and SQLite cheerfully creates a ``$HOME`` directory next to CWD.
_NODE_TRACE_DB_PATH = os.path.expanduser(os.path.expandvars(
    os.environ.get("NODE_TRACE_DB", "logs/node_traces.db")
))
_NODE_TRACE_STORE: NodeTraceStore | None = (
    NodeTraceStore(db_path=_NODE_TRACE_DB_PATH)
    if os.environ.get("NODE_TRACE_DISABLE") != "1"
    else None
)
if _NODE_TRACE_STORE is not None:
    # Print the resolved path at import time so a mismatch between
    # server and viewer DBs is immediately obvious. The viewer prints
    # the same thing on startup — if they don't match, you'll see two
    # different paths and know to align the NODE_TRACE_DB env var.
    print(
        f"[server] node_trace db: {Path(_NODE_TRACE_DB_PATH).resolve()}",
        flush=True,
    )
else:
    print("[server] node_trace disabled (NODE_TRACE_DISABLE=1)", flush=True)

# Cap on how long a queued new turn waits for the prior turn's
# `turn_lock` to release. Previously the queued runner did an
# unbounded `lock.acquire()` after a 2s contention log — so a new
# question after a hung prior turn could wait 30s (screen fence) /
# 240s (specialist fence) / 360s (turn fence) before getting any
# chance to run. With cooperative cancellation (via
# `sess.cancel_in_flight`) the prior turn aborts at its next
# phase-boundary so the typical wait is much shorter, but this cap
# is the belt-and-suspenders: if cancellation doesn't reach a
# checkpoint within 90s, surface a clear error to the user instead
# of holding their browser frozen.
_QUEUED_TURN_MAX_WAIT_S = float(
    os.environ.get("QUEUED_TURN_MAX_WAIT_S", "90")
)


class _TurnAborted(Exception):
    """Raised from `_run_turn_streamed` checkpoints when the session's
    `cancel_in_flight` event is set (typically by `post_rewind`). The
    `_spawn_turn._runner` outer wrapper catches it, logs the abort,
    emits a `turn_done` with `outcome="aborted"`, and releases the
    lock so the next queued turn can proceed."""


# ── Per-case session state ──────────────────────────────────────────────────

@dataclass
class CaseSession:
    """In-memory session for one case. Holds orchestrator state and SSE subscribers."""

    case_id: str
    gateway: LocalDataGateway
    catalog: DataCatalog
    clients: Any
    pillar_yaml: dict
    chat_agent: ChatAgent
    logger: EventLogger
    # Conversation memory across turns: each turn appends to this list, the next
    # `Runner.run_streamed` is invoked with the full list as input.
    input_history: list = field(default_factory=list)
    # Current turn lock — serialize turns per case. The frontend disables the
    # composer while a turn is in flight.
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    # Cooperative-cancellation signal for the in-flight turn. Set by
    # `post_rewind` when the user rewinds while a turn is still running,
    # so the in-flight `_run_turn_streamed` can abort at its next
    # phase-boundary checkpoint rather than letting the new (queued)
    # turn wait for the dead turn's fences to fire. Cleared by the
    # NEXT turn's runner once it acquires the lock — so the signal
    # only ever applies to "whoever is currently mid-flight".
    cancel_in_flight: threading.Event = field(default_factory=threading.Event)
    # (event_loop, task) tuple for the currently-running turn, or None.
    # Set by `_spawn_turn._runner` after building the per-turn asyncio
    # loop+task; cleared in the same runner's `finally`. `post_rewind`
    # reads this and uses `loop.call_soon_threadsafe(task.cancel)` to
    # propagate a CancelledError INTO the task — which interrupts the
    # in-flight LLM call directly at the underlying `httpx` await,
    # not just at the next Python-level checkpoint. Pair with the
    # cooperative `cancel_in_flight` signal: aggressive task.cancel()
    # handles the await-mid-LLM case; the event handles purely-sync
    # code paths that don't yield to the loop. Race-safe by design
    # — calling `task.cancel()` on a completed task is a no-op.
    current_inflight: Any = None
    # SSE subscribers — each one owns a queue.Queue of (event_name, payload).
    subscribers: list[queue.Queue] = field(default_factory=list)
    subscribers_lock: threading.Lock = field(default_factory=threading.Lock)
    # Ring buffer of recent (event_name, payload) frames, replayed to a newly
    # (re)connected subscriber so a turn that ran while disconnected isn't
    # lost. Guarded by `subscribers_lock`. Cleared on /rewind.
    event_buffer: deque = field(
        default_factory=lambda: deque(maxlen=_EVENT_BUFFER_MAX)
    )
    # Per-session exact-match Q→A cache. Keyed by `_normalize_q(redacted_question)`;
    # value carries the cached FinalAnswer fields. Skips orchestrator on repeats.
    qa_cache: dict = field(default_factory=dict)
    # Per-specialist KNOWLEDGE BASE — survives across turns within this session.
    # Keyed by specialist name; each value is a chronological list of
    # KnowledgePoint dicts produced by the distiller agent after each
    # specialist run. Older entries are RETAINED for audit when a newer KP
    # supersedes them; the active set is "latest per topic" (filter happens in
    # redacting_tool._format_kb_digest). Cleared by /rewind alongside
    # input_history and qa_cache so a session reset wipes everything.
    specialist_kb: dict = field(default_factory=dict)

    def emit(self, event_name: str, payload: dict) -> None:
        """Fan out an SSE event to every subscriber of this case."""
        event = (event_name, payload)
        with self.subscribers_lock:
            # Buffer for replay to clients that (re)connect later. Skip pings —
            # they're pure keepalive with no state to restore.
            if event_name != "ping":
                self.event_buffer.append(event)
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # A slow/stale client should not let its queue retain an
                    # unbounded backlog. Drop the oldest frame and keep the
                    # newest state moving.
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(event)
                    except queue.Full:
                        pass


SESSIONS: dict[str, CaseSession] = {}
SESSIONS_LOCK = threading.Lock()


# ── Bootstrap (shared across all sessions) ──────────────────────────────────

print(f"[server] resolving data source: {DATA_SOURCE}")
_source, _csv_dir = _resolve_data_source(DATA_SOURCE, _DATA_TABLES_DIR)
print(f"[server] data source: {_source} ({_csv_dir})")

if _source == "generator":
    from datalayer.generator import DataGenerator
    _gen = DataGenerator(seed=42, cases=10)
    _gen.load_profiles()
    _tables_raw = _gen.generate_all()
    _GATEWAY = LocalDataGateway.from_generated(_tables_raw)
else:
    _GATEWAY = LocalDataGateway.from_case_folders(str(_csv_dir))

_CATALOG = DataCatalog()
_BOOT_LOGGER = EventLogger(session_id=f"server-{uuid.uuid4().hex[:8]}")
init_tools(_GATEWAY, _CATALOG, logger=_BOOT_LOGGER)

_FIREWALL = FirewallStack(
    logger=_BOOT_LOGGER,
      specialist_concurrency=12,
      orchestrator_concurrency=2
      )
_CLIENTS = build_session_clients(_FIREWALL, model_name=MODEL, backend=None)
_CHAT_LLM = FirewalledChatShim(_CLIENTS)


def _prewarm_clients() -> None:
    """Fire one cheap LLM call at server bootstrap to pay safechain's
    cold-start cost BEFORE the first user request.

    Without this, the first question after `python server.py` makes the
    user pay the safechain init bill (~10-30s for `_refresh_llm()` +
    HTTP-pool warmup + auth token acquisition) inline — frequently
    enough that the `_SCREEN_TIMEOUT_S=30` fence trips on cold-start
    even on a healthy server. Pre-warming moves that cost to startup
    where slow is acceptable. User-facing screens stay ~2-5s.

    Default: ON for safechain backend (`LLM_BACKEND=safechain`), OFF
    for OpenAI (sub-second cold-start there, prewarm is wasted work).
    Override either way via `LLM_PREWARM=1` / `LLM_PREWARM=0`.

    Failure is non-fatal: a failed prewarm logs the error but the
    server keeps starting. The first user request will then pay the
    cold-start cost as before — strictly no worse than not pre-warming.
    """
    backend = getattr(_CLIENTS, "backend", None) or os.environ.get("LLM_BACKEND", "openai")
    default_on = backend == "safechain"
    env_val = os.environ.get("LLM_PREWARM")
    if env_val is not None:
        enabled = env_val.lower() in ("1", "true", "yes", "on")
    else:
        enabled = default_on
    if not enabled:
        _BOOT_LOGGER.log("llm_prewarm_skipped", {"backend": backend})
        print(f"[server] LLM prewarm skipped (backend={backend})")
        return

    print(f"[server] pre-warming LLM client (backend={backend}) …")
    _BOOT_LOGGER.log("llm_prewarm_start", {"backend": backend})
    t0 = time.time()

    async def _warmup_call() -> None:
        # Minimal prompt that exercises: token acquisition + HTTP pool
        # warmup + safechain LCEL model load (for safechain) or
        # AsyncOpenAI httpx pool init (for OpenAI). The reply is
        # discarded — we only care about the side effect of touching
        # every layer once.
        await _CLIENTS.firewalled_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "ping"}],
        )

    try:
        asyncio.run(asyncio.wait_for(_warmup_call(), timeout=60.0))
        elapsed_ms = int((time.time() - t0) * 1000)
        _BOOT_LOGGER.log("llm_prewarm_done", {
            "backend": backend, "duration_ms": elapsed_ms,
        })
        print(f"[server] LLM prewarm done in {elapsed_ms}ms")
    except Exception as exc:  # noqa: BLE001 — prewarm is best-effort
        elapsed_ms = int((time.time() - t0) * 1000)
        _BOOT_LOGGER.log("llm_prewarm_failed", {
            "backend": backend,
            "duration_ms": elapsed_ms,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
        })
        print(f"[server] LLM prewarm failed after {elapsed_ms}ms: "
              f"{type(exc).__name__}: {str(exc)[:200]}")
        print("[server] continuing startup — first user question will "
              "pay the cold-start cost.")


_prewarm_clients()


_PILLAR_YAML = PillarLoader().load(PILLAR) or {}
_HELPER_TOOLS = build_helper_tools()

ALL_CASES = _GATEWAY.list_case_ids()
print(f"[server] {len(ALL_CASES)} cases available: {ALL_CASES[:5]}{'...' if len(ALL_CASES) > 5 else ''}")


def _synthesize_fallback_answer(
    tool_calls: list[dict],
    error_kind: str,
    error_message: str,
) -> tuple[str, list[str]]:
    """Build a best-effort answer from the specialist outputs we have when the
    orchestrator's final synthesis fails (e.g. ModelBehaviorError on FinalAnswer
    parsing — the model emitted truncated/malformed JSON).

    Without this fallback, every specialist run is wasted because the SDK
    raises before ``streamed.final_output`` is populated. We salvage the
    individual SpecialistOutput payloads we already streamed and present them
    as a bulleted "what each specialist found" block so the reviewer at least
    sees the underlying findings.

    Returns ``(answer_markdown, flags)``. The flags carry the structured
    failure cause so it lands in the FinalAnswer audit trail too.
    """
    _AUX_TOOLS = {"report_agent", "general_specialist"}

    def _excerpt(payload) -> str:
        """Pull the most-readable field from a specialist payload, capped."""
        if payload is None:
            return "(no payload)"
        # Specialists typically return SpecialistOutput {answer, findings,
        # data_gap, ...}. After redact_payload + _safe_dump it's a dict; on
        # failure paths it's a "[FAILED …]" string.
        if isinstance(payload, str):
            return payload[:600]
        if isinstance(payload, dict):
            for key in ("answer", "findings", "summary", "data_gap"):
                v = payload.get(key)
                if v:
                    return (str(v) if not isinstance(v, str) else v)[:600]
            # No known field — dump compactly.
            try:
                return json.dumps(payload, default=str)[:600]
            except Exception:
                return str(payload)[:600]
        return str(payload)[:600]

    successful = [c for c in tool_calls if "payload" in c]
    domain_results = [c for c in successful if c["tool"] not in _AUX_TOOLS]
    aux_results = [c for c in successful if c["tool"] in _AUX_TOOLS]

    lines = [
        "**The agent could not produce a synthesized answer for this turn.** "
        "The orchestrator's final-output step failed before it could combine "
        "the specialists' findings. Below is what each specialist returned "
        "this run — review them directly.",
        "",
    ]
    if domain_results:
        lines.append("**Specialist findings**")
        for c in domain_results:
            lines.append(f"- **{c['tool']}** — {_excerpt(c.get('payload'))}")
        lines.append("")
    if aux_results:
        lines.append("**Reports / cross-domain review**")
        for c in aux_results:
            lines.append(f"- **{c['tool']}** — {_excerpt(c.get('payload'))}")
        lines.append("")
    if not successful:
        lines.append(
            "_No specialists produced a result before the orchestrator failed._"
        )

    lines.extend([
        "---",
        f"_Error category: `{error_kind}`. Re-ask the question (often a "
        f"transient model-output issue) or narrow the scope — e.g. ask about "
        f"one domain at a time._",
    ])

    flags = [
        f"orchestrator_failed: {error_kind}",
        f"fallback_answer: synthesized from {len(domain_results)} specialist(s)",
    ]
    return "\n".join(lines), flags


def _replay_completed_specialists(sess, turn_id: str, tool_calls: list[dict]) -> None:
    """Re-emit ``team_plan`` + ``agent_completed`` for specialists that produced
    a payload this turn.

    Error / fallback branches that emit ``final`` MUST call this first, or the
    reasoning-trace panel loses the work that DID succeed: a turn-wide ``error``
    leaves completed specialists rendered as "failed" with no trace (see the
    ``alternate_paths_must_replay_full_sse`` project rule). ``agent_completed``
    carries the payload and the frontend upserts by ``call_id``, so re-emitting
    is idempotent on the success path and restorative on the failure path.
    """
    completed = [c for c in tool_calls if "payload" in c]
    if not completed:
        return
    sess.emit("team_plan", {"turn_id": turn_id, "tool_calls": list(tool_calls)})
    for c in completed:
        sess.emit("agent_completed", {
            "turn_id": turn_id,
            "call_id": c.get("call_id"),
            "tool": c.get("tool"),
            "payload": c.get("payload"),
            "duration_ms": c.get("duration_ms", 0),
        })


    # (input_history pruning removed — each turn now starts fresh.
    #  Follow-up context lives in specialist_kb + KB warmth hint.)


def _normalize_q(q: str) -> str:
    """Normalize a question for the per-session exact-match QA cache.

    Lowercase, strip outer whitespace, collapse internal whitespace. The
    cache is intentionally exact-match-after-redaction (run_normalize on
    `verdict.redacted_question`); fuzzy similarity is the orchestrator's
    job (team_construction.md), not the cache's.
    """
    return " ".join((q or "").strip().lower().split())


def _get_cached_qa(sess: CaseSession, cache_key: str | None) -> dict | None:
    """Return a QA-cache entry and refresh its insertion order.

    ``dict`` preserves insertion order on supported Python versions, so a
    pop/reinsert gives us LRU behavior without changing the stored type or
    existing tests/fixtures.
    """
    if not cache_key:
        return None
    cached = sess.qa_cache.get(cache_key)
    if cached is None:
        return None
    try:
        sess.qa_cache[cache_key] = sess.qa_cache.pop(cache_key)
    except Exception:
        pass
    return cached


def _store_cached_qa(sess: CaseSession, cache_key: str | None, value: dict) -> int:
    """Store a QA-cache entry and evict oldest entries beyond the cap.

    Returns the number of entries evicted. The cache is a speed optimization,
    not the audit source, so bounding it avoids long sessions retaining every
    answer payload forever.
    """
    if not cache_key:
        return 0
    sess.qa_cache[cache_key] = value
    evicted = 0
    while _QA_CACHE_MAX_ENTRIES > 0 and len(sess.qa_cache) > _QA_CACHE_MAX_ENTRIES:
        try:
            oldest = next(iter(sess.qa_cache))
        except StopIteration:
            break
        sess.qa_cache.pop(oldest, None)
        evicted += 1
    return evicted


# ── Phase 2 / 3 helpers — viz embedding + KB warmth ────────────────────────


def _format_kb_warmth_hint(specialist_kb: dict) -> str:
    """Build the `[KB-warmth: …]` preface the orchestrator sees on every turn
    after the first one.

    Lists each specialist with non-empty KB, its topic names, and one-line
    claims — the orchestrator uses this for:
      1. Routing: reuse warm specialists for in-domain follow-ups.
      2. Sub-question framing: reference cached data in the sub-question so
         the specialist (or the orchestrator itself) can skip re-querying.
         E.g. "TSR breached threshold in 2024-08..2024-10 (per KB). What
         strategy actions coincided with those months?"

    Returns "" when no specialist has any KPs (e.g. first turn).
    """
    if not isinstance(specialist_kb, dict) or not specialist_kb:
        return ""
    lines: list[str] = []
    for name in sorted(specialist_kb):
        kps = specialist_kb[name]
        if not kps:
            continue
        active: dict[str, dict] = {}
        for kp in kps:
            topic = kp.get("topic")
            if topic:
                active[topic] = kp
        if not active:
            continue
        topic_lines = []
        for topic, kp in active.items():
            claim = (kp.get("claim") or "")[:120]
            topic_lines.append(f"    - {topic}: {claim}")
        lines.append(f"  {name} ({len(active)} KPs):")
        lines.extend(topic_lines)
    if not lines:
        return ""
    return (
        "[KB-warmth — cached specialist knowledge from prior turns. "
        "Use topic details to anchor sub-questions and avoid redundant queries:\n"
        + "\n".join(lines)
        + "\n"
        + "Reuse warm specialists for in-domain follow-ups. "
        + "Reference specific cached findings in sub-questions when relevant.]"
    )


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN / Infinity) with None.

    Python's ``json.dumps`` emits NaN/Infinity as bare ``NaN`` / ``Infinity``
    tokens — which are INVALID JSON. The browser's ``JSON.parse`` rejects them,
    so the frontend SSE handler drops the ENTIRE event. This silently lost
    `chart` payloads (vega specs / numeric cells / computed stats can be
    non-finite) while leaving text events (`final`, `turn_done`) untouched —
    the intermittent "chart missing until re-ask" bug. Coercing to null keeps
    the frame valid JSON; the client renders a gap rather than nothing.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _sse_data(payload: dict) -> str:
    """Serialize an SSE payload to a guaranteed browser-parseable JSON string."""
    return json.dumps(_json_safe(payload), default=str)


class _PlanningTimeout(Exception):
    """Round-1 (team-planning) stalled past ORCH_PLAN_TIMEOUT_S — abort + retry."""


async def _next_planning_event(stream_iter, plan_deadline, first_tool_seen):
    """Await the next orchestrator stream event, bounded by the planning deadline.

    While the orchestrator is still PLANNING (no tool call emitted yet) and a
    ``plan_deadline`` (``time.monotonic`` seconds) is armed, bound the wait by
    the remaining budget and raise ``_PlanningTimeout`` if it elapses — so a
    stalled round-1 call is cancelled and retried with a fresh request instead
    of dead-waiting. Once the first tool call has landed (``first_tool_seen``)
    the deadline is disarmed and later events stream under the normal turn
    wall-clock fence. ``plan_deadline=None`` (the final attempt) also disarms it,
    so a genuinely slow-but-working env degrades to waiting, not hard failure.
    """
    if plan_deadline is not None and not first_tool_seen:
        budget = plan_deadline - time.monotonic()
        if budget <= 0:
            raise _PlanningTimeout()
        try:
            return await asyncio.wait_for(stream_iter.__anext__(), timeout=budget)
        except asyncio.TimeoutError as exc:
            raise _PlanningTimeout() from exc
    return await stream_iter.__anext__()


def _collect_turn_charts(specialist_kb: dict, turn_id: str, case_id: str) -> list[dict]:
    """Find every KP captured in this turn that surfaces in the Plots panel.

    Two kinds of KP qualify:
      1. Rendered charts — KPs with an ``image_path`` set by
         ``render_chart``. Returned with ``url`` pointing at the Flask
         route ``/api/cases/<case_id>/charts/<filename>``.
      2. Table KPs — ``viz.kind == "table"`` (no image; the rows are
         shown as an HTML table in the panel). Returned with empty
         ``url`` and ``numbers`` carrying the row data.

    Deduped by ``(specialist, topic)`` — when both the `make_chart` tool
    and the auto-distiller produce an entry for the same topic in one
    turn, the latest one wins (chronological iteration order).
    """
    if not isinstance(specialist_kb, dict):
        return []
    by_key: dict[tuple[str, str], dict] = {}
    for spec_name, kps in specialist_kb.items():
        if not isinstance(kps, list):
            continue
        for kp in kps:
            if not isinstance(kp, dict):
                continue
            if kp.get("captured_at_turn") != turn_id:
                continue
            img_path = kp.get("image_path")
            viz = kp.get("viz") or {}
            kind = viz.get("kind", "") if isinstance(viz, dict) else ""
            is_table = kind == "table"
            if not img_path and not is_table:
                continue
            topic = kp.get("topic", "chart")
            entry: dict = {
                "topic": topic,
                "specialist": spec_name,
                "url": "",
            }
            if img_path:
                entry["url"] = f"/api/cases/{case_id}/charts/{Path(img_path).name}"
            # Latest wins per (specialist, topic). Iteration order over
            # the KB's chronological list means the last appended entry
            # naturally overwrites the earlier one for the same key.
            by_key[(spec_name, topic)] = entry
    return list(by_key.values())


def _append_charts_to_answer(answer_text: str, charts: list[dict]) -> str:
    """DEPRECATED — superseded by the typed ``chart`` SSE event surfaced in
    the reasoning-trace panel. Retained for backward compat with any
    external caller; new code should NOT inline charts in the chat answer.
    """
    if not charts:
        return answer_text or ""
    body = (answer_text or "").rstrip()
    section = ["", "---", "", "**Supporting charts**", ""]
    for c in charts:
        section.append(f"![{c['topic']}]({c['url']})")
    return body + "\n" + "\n".join(section)


def _detect_missing_reanswers(tool_calls: list[dict]) -> list[dict]:
    """Return one entry per `corrected_specialist` flagged by
    `general_specialist` that did NOT get re-invoked AFTER the
    general_specialist's tool call in the same turn.

    Per the orchestrator's HARD GATE protocol Round 2.5: when
    general_specialist's ``resolved`` list contains a Resolution with
    ``corrected_specialist`` set, the orchestrator must re-invoke that
    specialist with the correction folded into the sub-question before
    emitting FinalAnswer. If the orchestrator skips that re-invocation,
    the pre-correction (wrong) specialist output flows into synthesis.

    Each returned dict carries the corrected_specialist name, the
    corrected_value general_specialist verified, and a short clip of
    the contradiction for the audit log.

    Defensive: tool_calls entries may not have a parseable payload yet
    (e.g., on partial stream completion), or general_specialist may have
    failed to produce a structured output — those just yield no
    missing-reanswer records (no false positives).
    """
    out: list[dict] = []
    for gs_idx, gs_call in enumerate(tool_calls):
        if gs_call.get("tool") != "general_specialist":
            continue
        payload = gs_call.get("payload")
        if not isinstance(payload, dict):
            continue
        resolved = payload.get("resolved") or []
        if not isinstance(resolved, list):
            continue
        # Tools called AFTER this general_specialist invocation are
        # candidate re-answers. We require the corrected specialist to
        # appear by NAME in that suffix.
        later_tools = {c.get("tool") for c in tool_calls[gs_idx + 1:]}
        for r in resolved:
            if not isinstance(r, dict):
                continue
            corrected = r.get("corrected_specialist")
            if not corrected:
                continue
            if corrected in later_tools:
                continue
            out.append({
                "corrected_specialist": corrected,
                # `corrected_value` is nullable per schema (null when the
                # resolution doesn't need a re-answer). `or ""` keeps the
                # downstream f-string from rendering the literal "None".
                "corrected_value": str(r.get("corrected_value") or "")[:200],
                "contradiction": str(r.get("contradiction") or "")[:200],
            })
    return out


def _find_kp(specialist_kb: dict, specialist: str, topic: str,
             turn_id: str) -> dict | None:
    """Return the latest KP for (specialist, topic) captured in this turn,
    or None when not present. Used to enrich the chart SSE event with the
    KP's claim / source_call / vega_spec."""
    if not isinstance(specialist_kb, dict):
        return None
    kps = specialist_kb.get(specialist) or []
    found: dict | None = None
    for kp in kps:
        if not isinstance(kp, dict):
            continue
        if kp.get("captured_at_turn") != turn_id:
            continue
        if kp.get("topic") != topic:
            continue
        found = kp  # latest-wins (chronological iteration)
    return found


def _split_cases(case_ids: list[str]) -> dict[str, list[str]]:
    """Heuristic split: C-* → consumer, M-* → commercial, else consumer."""
    consumer: list[str] = []
    commercial: list[str] = []
    for cid in case_ids:
        s = str(cid)
        if s.upper().startswith("M-") or s.upper().startswith("M"):
            # 'M*' is commercial only if we don't have any 'C-' prefix on others;
            # heuristic kept conservative: prefix-only.
            if s.upper().startswith("M-"):
                commercial.append(s)
            else:
                consumer.append(s)
        elif s.upper().startswith("C-"):
            consumer.append(s)
        else:
            consumer.append(s)
    return {"consumer": consumer, "commercial": commercial}


def _sync_case_catalog(case_id: str, gateway, catalog, logger) -> None:
    """Reconcile the canonical catalog against this case's actual CSV columns.

    Runs once per case at first-open. Pure in-memory: auto-aliased entries
    and observed-category drift land on `catalog._profiles[table]` so the
    specialists' `get_table_schema` returns case-accurate column resolutions
    (real CSV headers ↔ canonical names, observed value vocabularies, etc.).
    YAMLs on disk are NOT touched — committing case-specific drift back to
    source-controlled profiles is still the interactive
    `python -m datalayer.sync` flow's job.

    Skips LLM drafting for speed (regex-based descriptions only) — the
    runtime path can't afford the 5-30s LLM round-trips on each first open.
    """
    from datalayer import adapter

    canonical = {t: catalog._profiles[t]["columns"] for t in catalog.list_tables()}
    try:
        diff = adapter.reconcile_case(gateway, canonical, case_id)
    except Exception as exc:
        logger.log("case_catalog_sync_failed",
                   {"case_id": case_id, "error": str(exc)})
        print(f"  ⚠ catalog sync failed for {case_id}: {exc}")
        return
    patches = adapter.apply_diff_in_memory(diff, catalog)
    logger.log("case_catalog_sync_done", {
        "case_id": case_id,
        "n_auto_aliased": len(diff.auto_aliased),
        "n_ambiguous": len(diff.ambiguous),
        "n_new_columns": len(diff.new),
        "n_new_tables": len(diff.new_tables),
        "n_value_drift": len(diff.value_drift),
        "tables_patched": sorted(patches.keys()),
    })
    print(
        f"  ✓ catalog synced for {case_id}: "
        f"{len(diff.auto_aliased)} auto-aliased, "
        f"{len(diff.new)} new col(s), "
        f"{len(diff.ambiguous)} ambiguous, "
        f"{len(diff.new_tables)} new table(s)"
    )


def _get_or_create_session(case_id: str) -> CaseSession:
    """Lazily build a CaseSession for this case_id."""
    with SESSIONS_LOCK:
        sess = SESSIONS.get(case_id)
        if sess is not None:
            return sess

        if case_id not in ALL_CASES:
            raise KeyError(f"unknown case_id: {case_id}")

        # Per-session gateway clone so set_case() doesn't cross-contaminate.
        # LocalDataGateway holds case-scoped state; one gateway per case is safest.
        case_gateway = _GATEWAY  # Single gateway, set_case() is idempotent per call
        case_gateway.set_case(case_id)

        case_logger = EventLogger(session_id=f"case-{case_id}-{uuid.uuid4().hex[:6]}")
        case_logger.log("case_session_open", {"case_id": case_id})

        # First-open: reconcile the canonical catalog against this case's
        # actual CSV columns so specialists' get_table_schema sees accurate
        # aliases + observed value vocabularies for THIS case. In-memory only.
        _sync_case_catalog(case_id, case_gateway, _CATALOG, case_logger)

        sess = CaseSession(
            case_id=case_id,
            gateway=case_gateway,
            catalog=_CATALOG,
            clients=_CLIENTS,
            pillar_yaml=_PILLAR_YAML,
            chat_agent=ChatAgent(
                _CHAT_LLM, case_logger,
                tools=_HELPER_TOOLS,
                pillar_config=_PILLAR_YAML,
                node_trace_store=_NODE_TRACE_STORE,
            ),
            logger=case_logger,
        )
        SESSIONS[case_id] = sess
        return sess


# ── Async streaming worker ──────────────────────────────────────────────────

async def _run_turn_streamed(
    sess: CaseSession, turn_id: str, question: str,
    started_at: int | None = None,
) -> None:
    """Run a single reviewer turn, emitting SSE events as the run progresses.

    Maps Agents-SDK RunItems to typed events the frontend understands:
      ToolCallItem        → team_plan (collected) + agent_started
      ToolCallOutputItem  → agent_completed
      MessageOutputItem   → ignored (final structured output is the answer)

    ``started_at`` is the ms-since-epoch timestamp from when the turn was
    received. ``_spawn_turn`` already emits the visible "new turn" events
    (reviewer_message, turn_started, empty team_plan) BEFORE acquiring the
    per-case turn lock so the frontend resets immediately even on lock
    contention; this function picks up after those have fired and uses
    ``started_at`` for duration math.
    """
    if started_at is None:
        started_at = int(time.time() * 1000)
    # Set the per-turn scope contextvar so `_open_node()` calls anywhere
    # downstream (chat_agent.screen, redacting_tool, orchestrator) can build
    # a NodeTrace without passing chat/case/turn IDs through every call site.
    TURN_SCOPE.set(TurnScope(
        chat_id=sess.logger.session_id,
        case_id=sess.case_id,
        turn_id=turn_id,
    ))
    turn_timer = ProcessTimer(
        sess.logger,
        "turn",
        turn_id=turn_id,
        case_id=sess.case_id,
    )

    # ── 1. Question check (screen + relevance) ─────────────────────────────
    # Build the list of prior reviewer questions in this session so the
    # relevance_check skill can flag near-duplicates (matched on subject +
    # time-range + scope). The qa_cache holds raw redacted-question strings
    # as values' "origin_question"; we surface those here.
    timer_t0 = time.perf_counter()
    prior_questions = [v.get("origin_question", "") for v in sess.qa_cache.values()]
    prior_questions = [q for q in prior_questions if q]
    # Cap to the N most-recent prior questions to keep the relevance_check
    # prompt bounded as the qa_cache fills over the session. qa_cache uses
    # LRU ordering (insertion order + move-to-end on hits), so the last N
    # entries are the most recently used / asked. See _PRIOR_QUESTIONS_FOR_SCREEN.
    if len(prior_questions) > _PRIOR_QUESTIONS_FOR_SCREEN:
        prior_questions = prior_questions[-_PRIOR_QUESTIONS_FOR_SCREEN:]
    turn_timer.record(
        "prior_question_scan",
        int((time.perf_counter() - timer_t0) * 1000),
        prior_questions=len(prior_questions),
        qa_cache_entries=len(sess.qa_cache),
    )
    screen_t0 = time.time()
    try:
        # Wrap the screen phase in a node trace so it appears in the
        # trace DB alongside the orchestrator/specialist nodes. The
        # chat_agent creates child nodes (chat.redact, chat.relevance_check)
        # inside; this parent groups them under one "screen" row.
        async with _open_node(_NODE_TRACE_STORE, "screen", depth=0):
            verdict = await asyncio.wait_for(
                sess.chat_agent.screen(question, prior_questions=prior_questions),
                timeout=_SCREEN_TIMEOUT_S,
            )
    except asyncio.TimeoutError:
        sess.logger.log("screen_timeout", {
            "turn_id": turn_id,
            "limit_s": _SCREEN_TIMEOUT_S,
            "prior_questions_sent": len(prior_questions),
        })
        turn_timer.summary(outcome="screen_timeout")
        sess.emit("turn_error", {
            "turn_id": turn_id,
            "message": (
                f"Question-check phase exceeded the {_SCREEN_TIMEOUT_S:.0f}s "
                f"budget — likely a transient LLM-backend stall. Try again; "
                f"if it recurs, clearing case history via the rewind action "
                f"can shrink the screen prompt and unstick it."
            ),
            "recoverable": False,
        })
        sess.emit("turn_done", {
            "turn_id": turn_id,
            "ended_at": int(time.time() * 1000),
            "duration_ms": int(time.time() * 1000) - started_at,
            "outcome": "screen_timeout",
        })
        return
    except Exception as exc:
        turn_timer.summary(outcome="screen_failed")
        sess.emit("turn_error", {"turn_id": turn_id, "message": f"screen failed: {exc}", "recoverable": True})
        sess.emit("turn_done", {"turn_id": turn_id, "ended_at": int(time.time() * 1000),
                                "duration_ms": int(time.time() * 1000) - started_at,
                                "outcome": "orchestrator_error"})
        return

    screen_duration_ms = int((time.time() - screen_t0) * 1000)
    sess.logger.log("turn_phase_screen_done", {
        "turn_id": turn_id,
        "duration_ms": screen_duration_ms,
        "passed": verdict.passed,
    })
    turn_timer.record("screen", screen_duration_ms, passed=verdict.passed)

    # Cooperative-cancellation checkpoint #1 (post-screen). If the user
    # rewound during the screen LLM call, the cancel_in_flight event was
    # set; abort cleanly here rather than continuing into the (often
    # much longer) orchestrator phase. See `_TurnAborted` docstring.
    if sess.cancel_in_flight.is_set():
        raise _TurnAborted("rewind during screen phase")

    in_scope = verdict.passed
    outcome_after_screen = "ok" if in_scope else "screen_rejected"
    sess.emit("question_check", {
        "turn_id": turn_id,
        "passed": verdict.passed,
        "reason": verdict.reason,
        "redacted_question": verdict.redacted_question,
        "in_scope": in_scope,
        "outcome": outcome_after_screen,
    })

    if not verdict.passed:
        # Treat reject as the final answer — emit synthesis + agent_message.
        rejection_text = f"[rejected] {verdict.reason}"
        ts = int(time.time() * 1000)
        sess.emit("final", {
            "turn_id": turn_id, "answer": rejection_text, "flags": [verdict.reason],
            "timeline": [], "data_pull_request": None,
        })
        sess.emit("agent_message", {
            "id": str(uuid.uuid4()), "role": "agent", "text": rejection_text,
            "timestamp": ts, "turn_id": turn_id,
        })
        sess.emit("turn_done", {"turn_id": turn_id, "ended_at": ts,
                                "duration_ms": ts - started_at, "outcome": "screen_rejected"})
        turn_timer.summary(outcome="screen_rejected")
        return

    # ── 1.5. Cache lookup — exact-match first, then near-duplicate ───────
    # Cache key uses the redacted-question normalized form so identical
    # questions with different identifiers (case IDs etc.) collide as
    # intended. Rejections are not cached.
    timer_t0 = time.perf_counter()
    cache_key = _normalize_q(verdict.redacted_question)
    cached = _get_cached_qa(sess, cache_key)
    cache_hit_kind = "exact" if cached is not None else None
    # Fall back to relevance_check's near-duplicate verdict — the LLM
    # judged this question a near-duplicate of an earlier one along
    # subject + time-range + scope dimensions. Look up that prior
    # question's cached answer.
    if cached is None and verdict.near_duplicate_of:
        near_dup_key = _normalize_q(verdict.near_duplicate_of)
        cached = _get_cached_qa(sess, near_dup_key)
        if cached is not None:
            cache_hit_kind = "near_duplicate"
            sess.logger.log("qa_cache_hit_near_duplicate", {
                "turn_id": turn_id,
                "matched_prior": verdict.near_duplicate_of,
                "match_reason": verdict.near_duplicate_reason,
            })
    turn_timer.record(
        "qa_cache_lookup",
        int((time.perf_counter() - timer_t0) * 1000),
        hit=cached is not None,
        kind=cache_hit_kind,
    )
    if cached is not None:
        sess.logger.log("qa_cache_hit", {
            "turn_id": turn_id, "norm_q": cache_key,
            "origin_turn_id": cached.get("turn_id_origin"),
            "kind": cache_hit_kind,
        })
        # Record a trace entry for cache-hit turns so the trace DB
        # has a row for every turn the frontend shows.
        async with _open_node(_NODE_TRACE_STORE, "cache_replay", depth=0):
            attach_tag("cache_hit", cache_hit_kind or "exact")
        cached_text = cached["answer"]
        # Annotate so the reviewer sees that this is a replay, not a
        # fresh run — keeps the answer faithful to the original (no
        # silent staleness) while saving the orchestrator round-trip.
        if cache_hit_kind == "near_duplicate":
            note = (
                f"\n\n*— Reused from a near-duplicate prior question this "
                f"session ({verdict.near_duplicate_reason or 'matched on subject + scope'}). "
                f"Original question: \"{verdict.near_duplicate_of}\". "
                f"No fresh data pull.*"
            )
        else:
            note = (
                "\n\n*— Reused from a prior identical question this session "
                "(no fresh data pull).*"
            )
        replayed_text = cached_text + note
        # Replay the prior turn's reasoning trace so the orchestrator-flow
        # / specialists panel populates on a cache hit. Without these
        # emits, the UI receives only `final` + `agent_message` and the
        # reviewer sees an answer appear with no trace of how it was
        # produced — indistinguishable from a silent failure.
        cached_tool_calls = cached.get("tool_calls") or []
        if cached_tool_calls:
            sess.emit("team_plan", {
                "turn_id": turn_id,
                "tool_calls": cached_tool_calls,
            })
            replay_ts = int(time.time() * 1000)
            for tc in cached_tool_calls:
                call_id = tc.get("call_id") or str(uuid.uuid4())
                sess.emit("agent_started", {
                    "turn_id": turn_id, "call_id": call_id,
                    "tool": tc.get("tool"),
                    "started_at": replay_ts,
                })
                sess.emit("agent_completed", {
                    "turn_id": turn_id, "call_id": call_id,
                    "tool": tc.get("tool"),
                    "payload": tc.get("payload"),
                    "duration_ms": tc.get("duration_ms", 0),
                })
        # Re-emit any charts the original turn produced, scoped to THIS
        # turn's id so the reasoning-trace panel shows them on the replay.
        # Without this, the cached-answer replay would render with no charts
        # — a regression vs the previous "charts inlined in answer_text"
        # behavior, since charts now live as separate SSE events.
        for c in cached.get("charts") or []:
            sess.emit("chart", {**c, "turn_id": turn_id})
        ts = int(time.time() * 1000)
        sess.emit("final", {
            "turn_id": turn_id, "answer": replayed_text,
            "flags": (cached.get("flags") or []) + ["cached_answer_replay"],
            "timeline": [],
            "data_pull_request": cached.get("data_pull_request"),
        })
        sess.emit("agent_message", {
            "id": str(uuid.uuid4()), "role": "agent",
            "text": replayed_text,
            "timestamp": ts, "turn_id": turn_id,
        })
        sess.emit("turn_done", {
            "turn_id": turn_id, "ended_at": ts,
            "duration_ms": ts - started_at, "outcome": "ok",
        })
        turn_timer.summary(outcome="qa_cache_hit", cache_hit_kind=cache_hit_kind)
        return

    # ── 2. Build a fresh orchestrator for this turn ───────────────────────
    timer_t0 = time.perf_counter()
    orchestrator = Orchestrator(
        llm=None, logger=sess.logger, registry=None,
        pillar=PILLAR, pillar_config=sess.pillar_yaml,
        catalog=sess.catalog, gateway=sess.gateway,
        clients=sess.clients,
    )
    case_folder = _REPORTS_DIR / sess.case_id
    # AppContext is per-turn, but two of its attributes (`_specialist_kb` and
    # `_distiller`) need to outlive a single turn. We pass:
    #   • specialist_kb: a SHARED REFERENCE to the session's KB dict — mutating
    #     it from inside redacting_tool persists to the next turn automatically.
    #   • distiller: the orchestrator's distiller_agent (stateless), used by
    #     redacting_tool for second-pass KP extraction.
    #   • turn_id: stamped onto each KP at distill time for audit / chronology.
    # Emit hook for tools that want to publish typed SSE events DURING the
    # run (not just at end-of-turn). Used today by `make_chart` to fire a
    # `chart_pending` event the moment a specialist starts plotting, so the
    # UI shows "working on plots" placeholders long before the actual
    # `chart` event lands. The closure stamps `turn_id` so tool callers
    # don't have to. Guard against `sess.emit` raising (closed connection,
    # etc.) so a streaming failure to one client never poisons the agent
    # run for the rest of the session.
    def _emit_event(event_name: str, payload: dict) -> None:
        try:
            sess.emit(event_name, {**payload, "turn_id": turn_id})
        except Exception:  # noqa: BLE001
            pass

    ctx = AppContext(
        gateway=sess.gateway,
        case_folder=case_folder,
        logger=sess.logger,
        _specialist_kb=sess.specialist_kb,
        _distiller=getattr(orchestrator, "distiller_agent", None),
        _turn_id=turn_id,
        _emit_event=_emit_event,
        _node_trace_store=_NODE_TRACE_STORE,
        _catalog=sess.catalog,
    )
    turn_timer.record(
        "orchestrator_context_build",
        int((time.perf_counter() - timer_t0) * 1000),
    )

    # Phase 3 — KB-warmth signal. When specialists have accumulated KPs from
    # earlier turns, prepend a one-line hint to the user question so the
    # orchestrator's team_construction step has a runtime signal that nudges
    # toward reusing warm specialists on in-domain follow-ups. The hint is
    # informational only — the orchestrator retains LLM judgment.
    timer_t0 = time.perf_counter()
    warmth_hint = _format_kb_warmth_hint(sess.specialist_kb)
    if warmth_hint:
        sess.logger.log("kb_warmth_hint_emitted", {
            "turn_id": turn_id,
            "warm_specialists": [
                {"name": n, "n_kps": len(kps)}
                for n, kps in sess.specialist_kb.items() if kps
            ],
            "hint_length": len(warmth_hint),
        })
        framed_question = f"{warmth_hint}\n\n{verdict.redacted_question}"
    else:
        framed_question = verdict.redacted_question

    # Each turn starts fresh — no accumulated conversation history.
    # Follow-up context is carried by:
    #   - KB warmth hint (which specialists have cached data)
    #   - specialist_kb (accessible via kb_lookup/kb_list_topics tools)
    #   - qa_cache (exact-match replay for duplicate questions)
    # This keeps input size constant across turns instead of growing.
    run_input = framed_question
    turn_timer.record(
        "memory_framing",
        int((time.perf_counter() - timer_t0) * 1000),
        input_history_len=0,
        warmth_hint_present=bool(warmth_hint),
    )

    # Cooperative-cancellation checkpoint #2 (pre-orchestrator). Catches
    # the case where the user rewound between screen-done and
    # orchestrator-start — abort before kicking off the (potentially
    # 4-minute) orchestrator run.
    if sess.cancel_in_flight.is_set():
        raise _TurnAborted("rewind before orchestrator start")

    # ── 3. Stream the orchestrator run ────────────────────────────────────
    # Mark when we hand off to the orchestrator so we can measure the gap
    # to the first tool call. That gap = the orchestrator's first LLM call
    # (the team-construction decision); when the user reports "slow to
    # arrive at team construction", THIS is the number to look at.
    orch_t0 = time.time()
    orch_perf_t0 = time.perf_counter()
    sess.logger.log("turn_phase_orchestrator_starting", {
        "turn_id": turn_id,
        "input_history_len": len(sess.input_history),
        "input_history_chars": sum(
            len(json.dumps(item, default=str)) for item in sess.input_history
        ) if sess.input_history else 0,
        "warmth_hint_present": bool(warmth_hint),
        "n_specialists_warm": sum(1 for kps in sess.specialist_kb.values() if kps),
    })
    call_index_by_id: dict[str, int] = {}  # call_id → index in tool_calls list
    tool_calls: list[dict] = []
    started_at_by_call: dict[str, int] = {}
    team_plan_emitted = False
    first_tool_call_logged = False
    # Cursor over ctx._specialist_errors so we emit a typed `error` SSE event
    # exactly once per failure, as soon as the redacting_tool wrapper records
    # it. Without this, the reviewer would only see a vague `agent_completed`
    # carrying a "[FAILED …]" string and have to read it to figure out what
    # went wrong.
    specialist_errors_emitted = 0

    def _drain_specialist_errors() -> None:
        nonlocal specialist_errors_emitted
        errors = getattr(ctx, "_specialist_errors", None) or []
        while specialist_errors_emitted < len(errors):
            err = errors[specialist_errors_emitted]
            sess.emit("turn_error", {
                "turn_id": turn_id,
                "specialist": err.get("specialist"),
                "error_type": err.get("error_type"),
                # Raw human message ONLY. The frontend composes the display from
                # the separate `specialist` (node key) + `error_type` (prefix)
                # fields — embedding them here too produced a doubled label
                # ("timeout: report_agent: timeout: …").
                "message": err.get("error_message"),
                "sub_question": err.get("sub_question"),
                "recoverable": True,
            })
            specialist_errors_emitted += 1

    def _safe_dump(obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if isinstance(obj, dict):
            return {k: _safe_dump(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_safe_dump(v) for v in obj]
        return obj

    final_answer: FinalAnswer | None = None

    # Retry loop: on ``ModelBehaviorError`` (typically a malformed FinalAnswer
    # — truncated JSON, schema validation failure, hallucinated tool name)
    # try the orchestrator run ONCE more before falling through to the
    # `_synthesize_fallback_answer` salvage path. Re-running re-invokes
    # specialists, but the per-specialist conversation history persists in
    # ``ctx._specialist_histories`` so warm specialists return faster on the
    # second pass, and the KB ``ctx._specialist_kb`` keeps round-1 KPs
    # available to the orchestrator's prompt.
    #
    # Frontend coordination: an ``orchestrator_retry`` SSE event fires
    # between attempts so the UI can reset the previous attempt's
    # mid-stream state (team_plan, agent_started) and replace it with
    # the new attempt's events under the same turn_id.
    _MAX_ORCH_ATTEMPTS = 2  # 1 initial + 1 retry
    _orch_attempt = 0
    while True:
        if _orch_attempt > 0:
            # Reset per-attempt SSE-emit state and the structured payload
            # accumulator. The cross-attempt state (specialist_kb,
            # specialist_histories on ctx) is intentionally preserved —
            # warm specialists return faster on retry.
            call_index_by_id.clear()
            tool_calls.clear()
            started_at_by_call.clear()
            team_plan_emitted = False
            first_tool_call_logged = False
            specialist_errors_emitted = 0
            final_answer = None
        try:
            async with _open_node(_NODE_TRACE_STORE, "orchestrator", depth=0) as orch_nt:
                attach_tag("streaming")
                # Stash the input on the wrapper row so clicking the
                # `orchestrator` row in the viewer shows what was first
                # asked. The per-LLM-call detail (round_1, round_2, …)
                # is captured by NodeTraceRunHooks below.
                if isinstance(run_input, list):
                    attach_io(messages_json=json.dumps(run_input, default=str))
                    attach_usage(prompt_excerpt=json.dumps(run_input, default=str)[:4000])
                else:
                    attach_io(messages_json=json.dumps(
                        [{"role": "user", "content": str(run_input)}],
                        default=str,
                    ))
                    attach_usage(prompt_excerpt=str(run_input))
                # Per-LLM-round capture via SDK hooks. Replaces the
                # contextvar-propagation path that doesn't reach the
                # streamed Runner's background task. Hooks fire on the
                # same task as the model call, so they always see the
                # right parent.
                trace_hooks = (
                    NodeTraceRunHooks(_NODE_TRACE_STORE, orch_nt)
                    if isinstance(orch_nt, NodeTrace) else None
                )
                streamed = Runner.run_streamed(
                    orchestrator.orchestrator_agent, run_input, context=ctx,
                    hooks=trace_hooks,
                )
                _orch_perf_t0 = time.perf_counter()
                _ttft_recorded = False
                # Round-1 planning watchdog: bound time-to-first-tool-call and
                # retry on stall. Disarm on the FINAL attempt (no deadline) so a
                # genuinely slow-but-working env waits rather than hard-failing.
                _plan_deadline = (
                    time.monotonic() + _ORCH_PLAN_TIMEOUT_S
                    if _orch_attempt + 1 < _MAX_ORCH_ATTEMPTS else None
                )
                _stream_iter = streamed.stream_events().__aiter__()
                while True:
                    try:
                        event = await _next_planning_event(
                            _stream_iter, _plan_deadline, first_tool_call_logged
                        )
                    except StopAsyncIteration:
                        break
                    except _PlanningTimeout:
                        # Cancel the stalled run so its background task can't
                        # later emit a ghost team for a turn we've moved past.
                        try:
                            streamed.cancel()
                        except Exception:  # noqa: BLE001
                            pass
                        raise
                    if not _ttft_recorded:
                        attach_latency(
                            ttft_ms=int((time.perf_counter() - _orch_perf_t0) * 1000),
                        )
                        _ttft_recorded = True
                    if event.type != "run_item_stream_event":
                        continue
                    item = event.item
                    raw = getattr(item, "raw_item", None)

                    if isinstance(item, ToolCallItem):
                        name = (
                            getattr(raw, "name", None)
                            or (raw.get("name") if isinstance(raw, dict) else None)
                            or "?"
                        )
                        call_id = (
                            getattr(raw, "call_id", None)
                            or (raw.get("call_id") if isinstance(raw, dict) else None)
                            or str(uuid.uuid4())
                        )
                        args_str = (
                            getattr(raw, "arguments", None)
                            or (raw.get("arguments") if isinstance(raw, dict) else "{}")
                        )
                        try:
                            args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
                        except json.JSONDecodeError:
                            args = {"raw": args_str}
                        sub_q = args.get("sub_question") or args.get("input") or json.dumps(args, default=str)

                        call_index_by_id[call_id] = len(tool_calls)
                        tool_calls.append({"call_id": call_id, "tool": name, "sub_question": sub_q})
                        started_at_by_call[call_id] = int(time.time() * 1000)

                        # The first tool call IS team construction — this is the
                        # gap the user reports as "time to team construction stage".
                        if not first_tool_call_logged:
                            sess.logger.log("turn_phase_first_tool_call", {
                                "turn_id": turn_id,
                                "duration_ms_since_orch_start":
                                    int((time.time() - orch_t0) * 1000),
                                "first_tool": name,
                            })
                            first_tool_call_logged = True

                        # First tool call → emit team_plan once (the orchestrator may add more
                        # later; we send team_plan again on subsequent calls for incremental UX).
                        team_plan_emitted = True
                        sess.emit("team_plan", {"turn_id": turn_id, "tool_calls": list(tool_calls)})
                        sess.emit("agent_started", {
                            "turn_id": turn_id, "call_id": call_id, "tool": name,
                            "started_at": started_at_by_call[call_id],
                        })

                    elif isinstance(item, ToolCallOutputItem):
                        call_id = (
                            getattr(raw, "call_id", None)
                            or (raw.get("call_id") if isinstance(raw, dict) else None)
                            or "?"
                        )
                        tool = "?"
                        if call_id in call_index_by_id:
                            tool = tool_calls[call_index_by_id[call_id]]["tool"]
                        payload = _safe_dump(item.output)
                        started_ts = started_at_by_call.get(call_id, int(time.time() * 1000))
                        duration_ms = int(time.time() * 1000) - started_ts
                        # Stash the payload back onto `tool_calls` so a late-stage
                        # orchestrator failure (ModelBehaviorError on FinalAnswer
                        # parsing, etc.) can still synthesize a partial fallback
                        # answer from the specialists' outputs the reviewer paid for.
                        if call_id in call_index_by_id:
                            tool_calls[call_index_by_id[call_id]]["payload"] = payload
                            tool_calls[call_index_by_id[call_id]]["duration_ms"] = duration_ms
                        sess.emit("agent_completed", {
                            "turn_id": turn_id, "call_id": call_id, "tool": tool,
                            "payload": payload, "duration_ms": duration_ms,
                        })
                        # If the redacting_tool wrapper recorded a failure for any
                        # specialist this run, fan out typed `error` events now so
                        # the UI can show the real cause beside the vague `[FAILED …]`
                        # payload it just received.
                        _drain_specialist_errors()
                        # Stamp the time of the LAST agent_completed so we can
                        # attribute the gap-to-end-of-stream to synthesis.
                        last_agent_completed_at = time.time()

                    elif isinstance(item, MessageOutputItem):
                        pass  # handled by .final_output below

            # Drain complete — pull the final structured output. The
            # gap between the last `agent_completed` and HERE is the
            # orchestrator's synthesis pass — the model generating the
            # FinalAnswer JSON. Log it as its own phase so slow
            # synthesis on simple questions is diagnosable from the
            # JSONL alone (was previously invisible — synthesis time
            # was bundled into `duration_ms` on `turn_done` along with
            # specialist runtime + end-of-turn drain).
            try:
                _last = locals().get("last_agent_completed_at")
                if isinstance(_last, (int, float)):
                    synth_ms = int((time.time() - _last) * 1000)
                    sess.logger.log("turn_phase_synthesis_done", {
                        "turn_id": turn_id,
                        "duration_ms": synth_ms,
                        "n_tool_calls": len(tool_calls),
                    })
            except Exception:
                pass
            final_raw = streamed.final_output
            try:
                final_answer = redact_payload(final_raw) if final_raw else None
            except Exception:
                final_answer = final_raw
            # NOTE: the orchestrator's per-LLM-round detail is captured
            # by NodeTraceRunHooks (passed into Runner.run_streamed
            # above). No need for synthetic team_construction / synthesis
            # rows anymore — round_1 IS team-construction, the last
            # round IS synthesis, both with real durations + full I/O.

            # No conversation history accumulation — each turn starts
            # fresh. Follow-up context lives in specialist_kb + warmth hint.

            # Guard: if orchestrator emitted zero tool calls (skipped
            # specialists entirely), retry once — this is a safechain
            # flake where tool_choice="required" was ignored.
            if (not tool_calls
                    and _orch_attempt + 1 < _MAX_ORCH_ATTEMPTS):
                _orch_attempt += 1
                sess.logger.log("orchestrator_retry_no_tools", {
                    "turn_id": turn_id,
                    "attempt": _orch_attempt,
                })
                tool_calls = []
                continue

            # Successful attempt — exit the retry loop.
            break

        except _PlanningTimeout:
            # Round-1 stalled. Count the attempt, tell the UI we're replanning,
            # and loop — the next attempt issues a fresh planning call (which,
            # per the heavy-traffic pattern, almost always returns fast). The
            # final attempt runs with the deadline disarmed, so we never hard
            # fail an env that's merely slow.
            _orch_attempt += 1
            turn_timer.record(
                "orchestrator_plan_timeout",
                int((time.perf_counter() - orch_perf_t0) * 1000),
                attempt=_orch_attempt,
                timeout_s=_ORCH_PLAN_TIMEOUT_S,
            )
            sess.logger.log("orchestrator_plan_timeout", {
                "turn_id": turn_id,
                "attempt": _orch_attempt,
                "timeout_s": _ORCH_PLAN_TIMEOUT_S,
            })
            sess.emit("orchestrator_retry", {
                "turn_id": turn_id,
                "attempt": _orch_attempt,
                "reason": "planning_timeout",
                "message": (
                    "Replanning — team planning was slow to respond; "
                    "retrying with a fresh request."
                ),
            })
            continue

        except AgentsException as exc:
            # Retry-once on ``ModelBehaviorError`` BEFORE falling through to
            # the fallback synthesis. A malformed FinalAnswer (truncated
            # JSON / schema validation failure / hallucinated tool name)
            # often clears on a fresh roll because the model's output is
            # non-deterministic. Other AgentsException subclasses (UserError,
            # guardrail tripwires) are not retried — they reflect a real
            # protocol or configuration problem, not transient malformity.
            if (
                isinstance(exc, ModelBehaviorError)
                and _orch_attempt + 1 < _MAX_ORCH_ATTEMPTS
            ):
                _orch_attempt += 1
                turn_timer.record(
                    "orchestrator_attempt_failed",
                    int((time.perf_counter() - orch_perf_t0) * 1000),
                    attempt=_orch_attempt,
                    exception_type=type(exc).__name__,
                )
                sess.logger.log("orchestrator_retry", {
                    "turn_id": turn_id,
                    "attempt": _orch_attempt,
                    "exception_type": type(exc).__name__,
                    "message": str(exc)[:300],
                    "n_tool_calls_completed": sum(
                        1 for c in tool_calls if "payload" in c
                    ),
                })
                sess.emit("orchestrator_retry", {
                    "turn_id": turn_id,
                    "attempt": _orch_attempt,
                    "reason": "model_behavior_error",
                    "message": (
                        "Retrying — the model's FinalAnswer didn't parse "
                        "(typically a transient JSON malformity)."
                    ),
                })
                continue  # back to top of while loop → reset state + rerun

            # Drain any specialist-level failures recorded before the orchestrator
            # itself died so the reviewer still sees what broke under the hood.
            _drain_specialist_errors()

            # Two failure modes converge here:
            #   • ModelBehaviorError — the model emitted text the SDK couldn't
            #     parse as FinalAnswer (truncated JSON, pseudo tool-call text,
            #     output-schema mismatch). The specialists' work IS valid; only
            #     the final synthesis is broken. Recoverable.
            #   • Other AgentsException — UserError, guardrail tripwires, etc.
            #     Generally not recoverable but we still surface what we have.
            is_model_behavior = isinstance(exc, ModelBehaviorError)
            kind = "model_behavior" if is_model_behavior else type(exc).__name__

            # Human-readable error for the SSE `error` event — strip the noisy
            # Pydantic v2 paragraph so the UI shows something a reviewer can act
            # on, not a 600-char schema dump.
            raw = str(exc)
            if is_model_behavior:
                short = (
                    "Orchestrator could not produce a valid final answer "
                    "(the model's output was malformed or truncated). "
                    "Returning a partial summary built from the specialists' "
                    "results that did succeed."
                )
            else:
                short = f"Orchestrator failed: {type(exc).__name__}: {raw.splitlines()[0][:200]}"

            sess.logger.log("orchestrator_exception", {
                "turn_id": turn_id,
                "exception_type": type(exc).__name__,
                "message": raw[:1000],
                "kind": kind,
                "n_tool_calls_completed": sum(1 for c in tool_calls if "payload" in c),
            })
            sess.emit("turn_error", {
                "turn_id": turn_id,
                "message": short,
                "kind": kind,
                "recoverable": is_model_behavior,
            })

            # Build a fallback FinalAnswer from whatever specialists DID return.
            # Without this the reviewer would see "(no answer produced)" plus the
            # raw exception, and the work the specialists did would be wasted.
            answer_text, fallback_flags = _synthesize_fallback_answer(
                tool_calls=tool_calls, error_kind=kind, error_message=raw,
            )
            flags = list(fallback_flags)

            # Append per-specialist failures and any protocol violations to flags
            # the same way the success path does, so the audit trail is uniform.
            specialist_failures = getattr(ctx, "_specialist_errors", None) or []
            for e in specialist_failures:
                flags.append(
                    f"specialist '{e['specialist']}' failed: "
                    f"{e['error_type']}: {e['error_message']}"
                )

            # Replay the completed specialists' traces BEFORE `final` so the
            # reasoning panel keeps the work that succeeded — otherwise the
            # turn-wide error renders a correct specialist as "failed" with no
            # trace (alternate_paths_must_replay_full_sse).
            _replay_completed_specialists(sess, turn_id, tool_calls)

            ts = int(time.time() * 1000)
            sess.emit("final", {
                "turn_id": turn_id, "answer": answer_text, "flags": flags,
                "timeline": [], "data_pull_request": None,
            })
            sess.emit("agent_message", {
                "id": str(uuid.uuid4()), "role": "agent", "text": answer_text,
                "timestamp": ts, "turn_id": turn_id,
            })
            sess.emit("turn_done", {
                "turn_id": turn_id, "ended_at": ts,
                "duration_ms": ts - started_at,
                "outcome": "orchestrator_error_fallback" if is_model_behavior
                           else "orchestrator_error",
            })
            turn_timer.summary(
                outcome="orchestrator_error_fallback" if is_model_behavior
                else "orchestrator_error",
                n_tool_calls=len(tool_calls),
            )
            return

    turn_timer.record(
        "orchestrator_stream",
        int((time.perf_counter() - orch_perf_t0) * 1000),
        n_tool_calls=len(tool_calls),
        n_attempts=_orch_attempt + 1,
    )

    # Drain any errors that landed after the last tool call (e.g., a parallel
    # specialist that recorded its failure between the final agent_completed
    # and stream end).
    _drain_specialist_errors()

    # ── 4. Emit final + chat agent message ────────────────────────────────
    # Guard: if orchestrator finished with zero tool calls (skipped all
    # specialists), the answer is ungrounded — treat as a failure and
    # log it. This happens on safechain where tool_choice="required"
    # is enforced via prompt text and the LLM sometimes ignores it.
    if not tool_calls and final_answer is not None:
        sess.logger.log("orchestrator_no_tool_calls", {
            "turn_id": turn_id,
            "answer_preview": str(getattr(final_answer, "answer", ""))[:200],
        })
        flags_pre = getattr(final_answer, "flags", []) or []
        flags_pre = list(flags_pre) + [
            "Orchestrator answered without calling any specialist — "
            "answer may be ungrounded. Numbers are NOT verified by live data."
        ]
        if hasattr(final_answer, "flags"):
            final_answer.flags = flags_pre

    if final_answer is None:
        # Orchestrator streamed cleanly but emitted no structured FinalAnswer
        # (e.g., the model returned an empty / non-parseable message that the
        # SDK swallowed). Use the same specialist-output salvage path as the
        # exception branch so the reviewer never sees a bare "(no answer
        # produced)" with the specialists' work thrown away.
        answer_text, fallback_flags = _synthesize_fallback_answer(
            tool_calls=tool_calls,
            error_kind="empty_final_answer",
            error_message="orchestrator produced no FinalAnswer",
        )
        flags: list[str] = list(fallback_flags)
        timeline: list = []
        data_pull = None
    elif hasattr(final_answer, "model_dump"):
        d = final_answer.model_dump()
        answer_text = d.get("answer", "")
        flags = d.get("flags", [])
        timeline = d.get("timeline", [])
        data_pull = d.get("data_pull_request")
    else:
        answer_text = getattr(final_answer, "answer", str(final_answer))
        flags = getattr(final_answer, "flags", [])
        timeline = getattr(final_answer, "timeline", [])
        data_pull = getattr(final_answer, "data_pull_request", None)

    # Server-side provenance: extract report_coverage and specialists from
    # tool_calls so the model doesn't waste tokens restating the full drafts.
    # The FinalAnswer schema tells the model to leave report_draft/team_draft
    # null; we populate provenance from the tool results.
    _AUX_TOOLS_PROV = {"report_agent", "general_specialist"}
    if hasattr(final_answer, "report_draft") and final_answer.report_draft is None:
        for tc in tool_calls:
            if tc.get("tool") == "report_agent" and tc.get("payload"):
                try:
                    import re as _re
                    cov_match = _re.search(r'"coverage"\s*:\s*"(\w+)"', tc["payload"])
                    if cov_match:
                        from models.types import ReportDraft
                        final_answer.report_draft = ReportDraft(
                            coverage=cov_match.group(1),
                            files_consulted=[],
                        )
                except Exception:
                    pass
    if hasattr(final_answer, "team_draft") and final_answer.team_draft is None:
        consulted = sorted(
            tc["tool"] for tc in tool_calls if tc["tool"] not in _AUX_TOOLS_PROV
        )
        if consulted:
            from models.types import TeamDraft
            final_answer.team_draft = TeamDraft(
                answer="", specialists_consulted=consulted,
            )

    # Specialist failure flags — make every wrapper-recorded failure visible
    # in the FinalAnswer so the reviewer sees, e.g., "specialist 'wcc' failed:
    # ModelBehaviorError: invalid JSON …" instead of the silent drop the SDK
    # would otherwise produce.
    specialist_failures = getattr(ctx, "_specialist_errors", None) or []
    if specialist_failures:
        failure_flags = [
            f"specialist '{e['specialist']}' failed: "
            f"{e['error_type']}: {e['error_message']}"
            for e in specialist_failures
        ]
        flags = list(flags or []) + failure_flags

    # Protocol check: when 2+ unique domain specialists were called, the
    # orchestrator MUST also have called general_specialist (per the
    # team_construction skill's "HARD GATE"). Surface a flag when it didn't —
    # the answer still ships so the reviewer isn't stonewalled, but the
    # violation is visible in the audit trail and the Flags section.
    _AUX_TOOLS = {"report_agent", "general_specialist"}
    unique_domain_specialists = {
        c["tool"] for c in tool_calls if c["tool"] not in _AUX_TOOLS
    }
    general_specialist_called = any(
        c["tool"] == "general_specialist" for c in tool_calls
    )
    if len(unique_domain_specialists) >= 2 and not general_specialist_called:
        # Log for audit but don't flag as a violation — the orchestrator
        # may have legitimately determined the specialists were orthogonal
        # (no shared concepts to compare). The protocol allows this.
        sess.logger.log("general_specialist_skipped", {
            "turn_id": turn_id,
            "n_domain_specialists": len(unique_domain_specialists),
            "domain_specialists": sorted(unique_domain_specialists),
        })
    elif len(unique_domain_specialists) == 1 and general_specialist_called:
        # Converse violation: general_specialist invoked on a 1-specialist
        # turn — wastes 30-60s and produces an empty ReviewReport (a single
        # specialist cannot disagree with itself). Flag so the LLM's
        # training signal sees this is wrong.
        violation_flag = (
            f"general_specialist invoked unnecessarily (protocol violation: "
            f"only 1 domain specialist ran — nothing to cross-compare)"
        )
        flags = list(flags or []) + [violation_flag]
        sess.logger.log("orchestrator_protocol_violation", {
            "turn_id": turn_id,
            "violation": "unnecessary_general_specialist",
            "n_domain_specialists": 1,
            "domain_specialists": sorted(unique_domain_specialists),
        })

    # Round 2.5 protocol check: when general_specialist's `resolved` entries
    # carry a `corrected_specialist`, the orchestrator MUST re-invoke that
    # specialist with the correction before finalizing (see HARD GATE block
    # in orchestrator_agent.py). If a re-invocation is missing, the
    # pre-correction (wrong) specialist output flowed into synthesis —
    # flag it so the reviewer sees the violation in the audit trail.
    missing_reanswers = _detect_missing_reanswers(tool_calls)
    if missing_reanswers:
        for mr in missing_reanswers:
            violation_flag = (
                f"re-answer not invoked (protocol violation: "
                f"general_specialist flagged `{mr['corrected_specialist']}` "
                f"for correction to `{mr['corrected_value']}` but the "
                f"specialist was not re-invoked with that correction)"
            )
            flags = list(flags or []) + [violation_flag]
        sess.logger.log("orchestrator_protocol_violation", {
            "turn_id": turn_id,
            "violation": "missing_reanswer",
            "n_missing": len(missing_reanswers),
            "missing": missing_reanswers,
        })

    # ── Emit final answer IMMEDIATELY — don't wait for distiller drain.
    # The user sees the text answer now; charts arrive asynchronously.
    timer_t0 = time.perf_counter()
    ts = int(time.time() * 1000)
    sess.emit("final", {
        "turn_id": turn_id, "answer": answer_text, "flags": flags,
        "timeline": timeline, "data_pull_request": _safe_dump(data_pull),
    })
    sess.emit("agent_message", {
        "id": str(uuid.uuid4()), "role": "agent", "text": answer_text,
        "timestamp": ts, "turn_id": turn_id,
    })
    turn_timer.record(
        "final_sse_emit",
        int((time.perf_counter() - timer_t0) * 1000),
    )

    # ── Drain distiller tasks, then emit charts. The text answer is
    # already visible to the user; charts populate the trace panel
    # asynchronously. If a new question arrives during the drain, the
    # drain timeout (60s) ensures we don't block forever.
    pending = getattr(ctx, "_pending_distillers", None) or []
    timer_t0 = time.perf_counter()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            sess.logger.log("distiller_drain_timeout", {
                "turn_id": turn_id,
                "n_pending": sum(1 for t in pending if not t.done()),
            })
    turn_timer.record(
        "distiller_drain",
        int((time.perf_counter() - timer_t0) * 1000),
        n_pending=len(pending),
        n_pending_unfinished=sum(1 for t in pending if not t.done()) if pending else 0,
    )

    # Collect and emit charts from the KB (now populated by the drain).
    timer_t0 = time.perf_counter()
    turn_charts = _collect_turn_charts(sess.specialist_kb, turn_id, sess.case_id)
    chart_payloads: list[dict] = []
    if turn_charts:
        for c in turn_charts:
            kp = _find_kp(sess.specialist_kb, c["specialist"], c["topic"], turn_id)
            viz = (kp or {}).get("viz") or {}
            payload = {
                "specialist": c["specialist"],
                "topic": c["topic"],
                "url": c["url"],
                "claim": (kp or {}).get("claim", ""),
                "source_call": (kp or {}).get("source_call", ""),
                "kind": viz.get("kind", "") if isinstance(viz, dict) else "",
                "vega_spec": (kp or {}).get("vega_spec"),
            }
            if payload["kind"] == "table":
                payload["numbers"] = (kp or {}).get("numbers") or []
                payload["x_field"] = viz.get("x_field", "") if isinstance(viz, dict) else ""
                payload["y_fields"] = (
                    viz.get("y_fields") or [] if isinstance(viz, dict) else []
                )
            chart_payloads.append(payload)
        for p in chart_payloads:
            sess.emit("chart", {**p, "turn_id": turn_id})
        sess.logger.log("turn_charts_emitted", {
            "turn_id": turn_id,
            "n_charts": len(chart_payloads),
            "topics": [p["topic"] for p in chart_payloads],
        })
    turn_timer.record(
        "chart_collect_emit",
        int((time.perf_counter() - timer_t0) * 1000),
        n_charts=len(chart_payloads),
    )

    # ── turn_done after charts are emitted.
    ts = int(time.time() * 1000)
    sess.emit("turn_done", {
        "turn_id": turn_id, "ended_at": ts,
        "duration_ms": ts - started_at, "outcome": "ok",
    })

    # Cache the answer for exact-match replay on identical follow-up
    # questions in this session. Skip when the run produced no real answer
    # (final_answer was None) so we don't poison the cache with the
    # "(no answer produced)" sentinel.
    if final_answer is not None and cache_key:
        timer_t0 = time.perf_counter()
        evicted_cache_entries = _store_cached_qa(sess, cache_key, {
            "answer": answer_text,
            "flags": list(flags or []),
            "data_pull_request": _safe_dump(data_pull),
            "turn_id_origin": turn_id,
            # Verbatim question text used by the relevance_check skill on
            # subsequent turns to spot near-duplicates of this one.
            "origin_question": verdict.redacted_question,
            # Chart payloads (turn_id-less) so cached-answer replays can
            # re-emit them under the new turn_id. PNG files persist under
            # reports/<case>/charts/ and serve fine on replay since the URL
            # is unchanged.
            "charts": chart_payloads,
            # Tool-call records (team_plan + each specialist's payload +
            # duration) so a cache-hit replay can repopulate the
            # orchestrator-flow / specialists panel. Without these, the
            # cached-answer replay arrives with an empty reasoning trace
            # and looks like a silent failure to the reviewer.
            "tool_calls": [
                {
                    "call_id": tc.get("call_id"),
                    "tool": tc.get("tool"),
                    "sub_question": tc.get("sub_question"),
                    "payload": tc.get("payload"),
                    "duration_ms": tc.get("duration_ms"),
                }
                for tc in tool_calls
            ],
        })
        sess.logger.log("qa_cache_store",
                        {"turn_id": turn_id, "norm_q": cache_key,
                         "answer_len": len(answer_text),
                         "entries_now": len(sess.qa_cache),
                         "entries_evicted": evicted_cache_entries})
        # Snapshot CaseSession's cross-turn state to the trace DB so the
        # viewer can show what the conversation "remembers" at end of turn:
        # qa_cache, specialist_kb (with all KnowledgePoints), input_history.
        # Failures are swallowed by the store; never breaks the turn.
        if _NODE_TRACE_STORE is not None:
            _NODE_TRACE_STORE.snapshot_session(
                chat_id=sess.logger.session_id,
                case_id=sess.case_id,
                turn_id=turn_id,
                qa_cache=sess.qa_cache,
                specialist_kb=sess.specialist_kb,
                input_history=sess.input_history,
            )
        turn_timer.record(
            "qa_cache_store",
            int((time.perf_counter() - timer_t0) * 1000),
            entries_now=len(sess.qa_cache),
            entries_evicted=evicted_cache_entries,
        )
    turn_timer.summary(
        outcome="ok",
        n_tool_calls=len(tool_calls),
        n_charts=len(chart_payloads),
    )


def _spawn_turn(sess: CaseSession, turn_id: str, question: str) -> None:
    """Run a turn in a background thread (Flask handlers must return promptly).

    Frontend-visible "this new turn started" SSE events fire BEFORE the
    per-case turn lock is acquired. Otherwise, when a previous turn is
    still running (or hung), the new turn's thread blocks on the lock and
    NO events for the new turn ever reach the frontend — the reasoning
    panel sticks on the previous turn's content because nothing scopes a
    new turn_id. Pre-lock emits guarantee the user sees their question
    appear and the reasoning panel reset, even if execution is queued.
    """
    started_at = int(time.time() * 1000)

    # Log subscriber count at turn start so the "server dropped" symptom
    # is diagnosable from the JSONL alone. When the count is 0, the
    # frontend's SSE stream isn't attached — the events this turn emits
    # will be enqueued but never delivered until the EventSource
    # reconnects. The frontend SHOULD auto-reconnect on transport error
    # (the load-bearing fix); this log just makes the failure visible
    # server-side.
    with sess.subscribers_lock:
        n_subs = len(sess.subscribers)
    if n_subs == 0:
        sess.logger.log("turn_no_subscribers", {
            "turn_id": turn_id,
            "case_id": sess.case_id,
            "note": ("turn dispatched with no attached SSE client — events "
                     "will be enqueued but undeliverable until reconnect. "
                     "Most likely the EventSource died during an idle "
                     "window (proxy idle-close, browser-tab throttle, "
                     "network blip)."),
        })
        print(f"[SSE] turn {turn_id} dispatched with 0 subscribers "
              f"(case={sess.case_id})")
    else:
        sess.logger.log("turn_subscribers_at_start", {
            "turn_id": turn_id,
            "n_subscribers": n_subs,
        })

    sess.emit("reviewer_message", {
        "id": str(uuid.uuid4()),
        "role": "reviewer",
        "text": question,
        "timestamp": started_at,
        "turn_id": turn_id,
    })
    sess.emit("turn_started", {
        "turn_id": turn_id,
        "question": question,
        "started_at": started_at,
    })
    # Empty team_plan resets the reasoning-panel scope to the new turn_id
    # immediately. Real tool calls re-emit team_plan with the actual list.
    sess.emit("team_plan", {"turn_id": turn_id, "tool_calls": []})

    def _runner():
        # Try to acquire the per-case turn lock; if a previous turn is
        # still in flight, log the contention but do NOT emit an SSE
        # `error` event. The frontend's error handler currently marks any
        # `error` event as a turn-fatal state, so a "queued" message would
        # paint this brand-new turn as failed before it runs. Just log;
        # the user has already seen `turn_started` for this turn and will
        # see real events flow once the lock releases.
        if not sess.turn_lock.acquire(timeout=2.0):
            sess.logger.log("turn_queued_waiting_lock", {"turn_id": turn_id})
            # Bounded wait: previously this was `lock.acquire()` with no
            # timeout, so a hung prior turn (waiting on its own 360s
            # fence) could block this queued turn for minutes. With
            # cooperative cancellation from rewind, the typical wait is
            # short, but the cap guarantees the user gets a clear error
            # in finite time rather than a frozen browser.
            if not sess.turn_lock.acquire(timeout=_QUEUED_TURN_MAX_WAIT_S):
                sess.logger.log("turn_queue_wait_timeout", {
                    "turn_id": turn_id,
                    "limit_s": _QUEUED_TURN_MAX_WAIT_S,
                })
                ts = int(time.time() * 1000)
                sess.emit("turn_error", {
                    "turn_id": turn_id,
                    "message": (
                        f"Prior turn on this case hasn't released after "
                        f"{_QUEUED_TURN_MAX_WAIT_S:.0f}s — try again, or "
                        f"clear history (rewind) to abort the prior turn."
                    ),
                    "recoverable": False,
                })
                sess.emit("turn_done", {
                    "turn_id": turn_id, "ended_at": ts,
                    "duration_ms": ts - started_at,
                    "outcome": "queue_timeout",
                })
                return
        # We hold the lock now — clear any leftover cancellation signal
        # from a prior turn so the new turn proceeds. The signal is
        # per-mid-flight, not per-session.
        sess.cancel_in_flight.clear()
        try:
            # `_run_turn_streamed` is structured to skip the early visible
            # emits when called from this path — see the `started_at`
            # parameter; the inner function uses that to drive duration
            # math without re-emitting reviewer_message / turn_started /
            # team_plan that we already fired above.
            #
            # Wall-clock fence (`_TURN_WALL_CLOCK_S`): without this, a
            # hung LLM HTTP call inside the orchestrator (DNS warmup, TLS
            # handshake, network blip) strands the per-case turn_lock
            # forever — every subsequent question on this case blocks on
            # the unbounded lock.acquire() above. On timeout we cancel
            # the coroutine (asyncio.wait_for raises TimeoutError), emit
            # a clear error to the user, and fall through to the
            # `finally` clause that releases the lock so the NEXT
            # question can proceed.
            # Use an explicit event loop instead of `asyncio.run` so
            # `post_rewind` can `loop.call_soon_threadsafe(task.cancel)`
            # to interrupt the in-flight LLM call mid-await. Without
            # this, cooperative cancellation only fires at Python-level
            # checkpoints — but the typical "stuck after rewind" case
            # has the turn parked inside an `httpx` await, which a
            # checkpoint can't interrupt. Direct task.cancel() does.
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                task = loop.create_task(asyncio.wait_for(
                    _run_turn_streamed(
                        sess, turn_id, question, started_at=started_at,
                    ),
                    timeout=_TURN_WALL_CLOCK_S,
                ))
                # Publish (loop, task) so post_rewind can cancel from
                # another thread. Race-safe — cancelling a completed
                # task is a no-op.
                sess.current_inflight = (loop, task)
                try:
                    loop.run_until_complete(task)
                except asyncio.TimeoutError:
                    sess.logger.log("turn_wall_clock_timeout", {
                        "turn_id": turn_id,
                        "limit_s": _TURN_WALL_CLOCK_S,
                        "note": ("turn exceeded the per-turn budget — most "
                                 "likely a hung orchestrator LLM call. "
                                 "Lock released; next question can run."),
                    })
                    ts = int(time.time() * 1000)
                    sess.emit("turn_error", {
                        "turn_id": turn_id,
                        "message": (
                            f"Turn exceeded the {_TURN_WALL_CLOCK_S:.0f}s "
                            f"budget — most likely a hung LLM call. The "
                            f"lock has been released so your next question "
                            f"can proceed."
                        ),
                        "recoverable": False,
                    })
                    sess.emit("turn_done", {
                        "turn_id": turn_id,
                        "ended_at": ts,
                        "duration_ms": ts - started_at,
                        "outcome": "timeout",
                    })
                except (asyncio.CancelledError, _TurnAborted) as exc:
                    # Either: aggressive task.cancel() from post_rewind
                    # or post_cancel_turn propagated as CancelledError,
                    # or a phase-boundary checkpoint raised _TurnAborted
                    # (cooperative path). Treat both the same.
                    #
                    # Emit `error` BEFORE `turn_done` so the frontend's
                    # orchestration-flow panel surfaces a clear
                    # interruption indicator (status='error', error=...)
                    # rather than silently transitioning the turn to
                    # 'done'. The frontend's turn_done handler is
                    # tweaked to NOT overwrite status='error' so the
                    # interruption stays visible until the next turn.
                    reason = "task_cancelled" if isinstance(exc, asyncio.CancelledError) else str(exc)
                    sess.logger.log("turn_aborted", {
                        "turn_id": turn_id,
                        "reason": reason,
                    })
                    ts = int(time.time() * 1000)
                    sess.emit("turn_error", {
                        "turn_id": turn_id,
                        "message": (
                            "Interrupted — the answer was stopped. "
                            "Ask another question whenever ready."
                        ),
                        "recoverable": False,
                        # `kind` lets the frontend distinguish "user
                        # pressed Stop" from "LLM hit a hard error". The
                        # orchestration flow can render a Stop icon
                        # instead of a red error chevron when kind is
                        # 'interrupted'.
                        "kind": "interrupted",
                    })
                    sess.emit("turn_done", {
                        "turn_id": turn_id,
                        "ended_at": ts,
                        "duration_ms": ts - started_at,
                        "outcome": "aborted",
                    })
            finally:
                # Drain leftover loop state: cancel any background tasks
                # still pending (e.g. fire-and-forget distillers that
                # didn't complete before cancellation), shut down async
                # generators, then close the loop. Same hygiene
                # `asyncio.run` does, exposed here because we manage the
                # loop manually.
                try:
                    pending = [
                        t for t in asyncio.all_tasks(loop) if not t.done()
                    ]
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(
                            *pending, return_exceptions=True,
                        ))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass
                sess.current_inflight = None
        finally:
            sess.turn_lock.release()
    threading.Thread(target=_runner, daemon=True, name=f"turn-{turn_id[:8]}").start()


# ── Flask app ───────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:5173", "http://127.0.0.1:5173"]}})


@app.get("/api/cases")
def get_cases():
    return jsonify(_split_cases(ALL_CASES))


@app.post("/api/cases/<case_id>/turn")
def post_turn(case_id: str):
    return _start_turn(case_id)


@app.post("/api/cases/<case_id>/message")
def post_message(case_id: str):
    """Legacy alias of /turn — preserves the existing React `postMessage` call."""
    return _start_turn(case_id)


def _start_turn(case_id: str):
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "missing text"}), 400
    try:
        sess = _get_or_create_session(case_id)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404

    turn_id = uuid.uuid4().hex[:12]
    _spawn_turn(sess, turn_id, text)
    return jsonify({"turn_id": turn_id}), 202


@app.post("/api/cases/<case_id>/cancel-turn")
def post_cancel_turn(case_id: str):
    """Interrupt the in-flight turn AND fully rewind — as if the
    stopped question was never asked.

    Clears: specialist_kb (current turn's KPs), qa_cache (so re-asking
    doesn't replay), input_history. Cancels the running task via
    aggressive task.cancel() + cooperative signal.
    """
    try:
        sess = _get_or_create_session(case_id)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404

    if not sess.turn_lock.locked():
        sess.logger.log("cancel_turn_noop", {"case_id": case_id})
        return jsonify({
            "status": "no_turn_in_flight",
            "task_cancel_dispatched": False,
        }), 200

    # Aggressive cancellation: inject CancelledError into the task
    cancelled_task = False
    inflight = sess.current_inflight
    if isinstance(inflight, tuple) and len(inflight) == 2:
        loop, task = inflight
        try:
            loop.call_soon_threadsafe(task.cancel)
            cancelled_task = True
        except Exception:
            pass
    sess.cancel_in_flight.set()

    # Full rewind: clear all session state from the stopped turn
    n_cached = len(sess.qa_cache)
    sess.qa_cache.clear()
    n_kb_total = sum(len(v) for v in sess.specialist_kb.values())
    sess.specialist_kb.clear()
    sess.input_history = []
    with sess.subscribers_lock:
        sess.event_buffer.clear()  # don't replay events from the wiped turn(s)

    # Clear rendered chart files
    n_charts_cleared = 0
    charts_dir = _REPORTS_DIR / case_id / "charts"
    if charts_dir.exists():
        for f in charts_dir.iterdir():
            if f.is_file() and f.suffix in (".svg", ".png"):
                try:
                    f.unlink()
                    n_charts_cleared += 1
                except Exception:
                    pass

    sess.logger.log("turn_cancelled_and_rewound", {
        "case_id": case_id,
        "task_cancel_dispatched": cancelled_task,
        "qa_cache_cleared": n_cached,
        "kb_entries_cleared": n_kb_total,
        "charts_cleared": n_charts_cleared,
    })
    return jsonify({
        "status": "cancelled_and_rewound",
        "task_cancel_dispatched": cancelled_task,
    }), 200


@app.post("/api/cases/<case_id>/rewind")
def post_rewind(case_id: str):
    body = request.get_json(silent=True) or {}
    msg_id = body.get("messageId", "")
    # Both rewind-to-point and clear-history reach this endpoint. We clear the
    # orchestrator's multi-turn input history AND the per-session exact-match
    # qa_cache so a previously-asked question does NOT replay its cached
    # answer when re-asked after a rewind. The front-end's clear-history
    # action also calls this for the active case (no message id), giving us
    # a single server-side reset path.
    try:
        sess = _get_or_create_session(case_id)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404

    remove_turn_ids: list[str] = body.get("removeTurnIds") or []
    is_partial = bool(remove_turn_ids)

    if is_partial:
        # Partial rewind: only remove specified turns from caches.
        # Drop qa_cache entries whose turn_id is in the removed set.
        removed_set = set(remove_turn_ids)
        n_cached = 0
        for key in list(sess.qa_cache):
            if sess.qa_cache[key].get("turn_id_origin") in removed_set:
                del sess.qa_cache[key]
                n_cached += 1
        # Drop KB entries produced during the removed turns.
        n_kb_total = 0
        for spec_name in list(sess.specialist_kb):
            before = len(sess.specialist_kb[spec_name])
            sess.specialist_kb[spec_name] = [
                kp for kp in sess.specialist_kb[spec_name]
                if kp.get("captured_at_turn") not in removed_set
            ]
            n_kb_total += before - len(sess.specialist_kb[spec_name])
        n_kb_specialists = sum(
            1 for kps in sess.specialist_kb.values() if kps
        )
    else:
        # Full rewind: clear everything.
        sess.input_history = []
        n_cached = len(sess.qa_cache)
        sess.qa_cache.clear()
        n_kb_specialists = len(sess.specialist_kb)
        n_kb_total = sum(len(v) for v in sess.specialist_kb.values())
        sess.specialist_kb.clear()

    # Drop replay-buffer frames for the rewound turn(s) so a later (re)connect
    # doesn't resurrect them.
    with sess.subscribers_lock:
        if is_partial:
            kept = [
                (n, p) for (n, p) in sess.event_buffer
                if not (isinstance(p, dict) and p.get("turn_id") in removed_set)
            ]
            sess.event_buffer.clear()
            sess.event_buffer.extend(kept)
        else:
            sess.event_buffer.clear()
    # Clear rendered chart files
    charts_dir = _REPORTS_DIR / case_id / "charts"
    if charts_dir.exists():
        for f in charts_dir.iterdir():
            if f.is_file() and f.suffix in (".svg", ".png"):
                try:
                    f.unlink()
                except Exception:
                    pass
    # If a turn is in flight when the user rewinds, abort it on two
    # tracks (defense in depth):
    #   1. Aggressive: `loop.call_soon_threadsafe(task.cancel)`
    #      propagates CancelledError INTO the asyncio task, which
    #      interrupts the current `httpx`/LLM await directly. This
    #      handles the typical "mid-LLM-call hang" case where a
    #      Python-level checkpoint can't fire because the task is
    #      parked inside a network read.
    #   2. Cooperative: setting `cancel_in_flight` so the next
    #      Python-level checkpoint inside `_run_turn_streamed`
    #      short-circuits. Handles the rare case where the task is
    #      inside synchronous code that doesn't yield to the loop
    #      (so direct cancel can't deliver until the next await).
    # Without either, the queued new question would wait up to the
    # 360s turn fence — visible user symptom: "I rewound and asked
    # again, but it's still stuck at screening."
    aborted_in_flight = sess.turn_lock.locked()
    cancelled_task = False
    if aborted_in_flight:
        inflight = sess.current_inflight
        if isinstance(inflight, tuple) and len(inflight) == 2:
            loop, task = inflight
            try:
                loop.call_soon_threadsafe(task.cancel)
                cancelled_task = True
            except Exception:
                # Loop already closed / task already done — fall back
                # to cooperative-only. The runner's `finally` clears
                # `current_inflight` so a stale handle is the only
                # plausible cause.
                pass
        sess.cancel_in_flight.set()
    # Wipe node trace rows so the trace DB stays in sync with the
    # frontend. Partial rewind deletes only the specified turns; full
    # rewind deletes everything for this case.
    trace_rows_cleared = 0
    if _NODE_TRACE_STORE is not None:
        if is_partial:
            trace_rows_cleared = _NODE_TRACE_STORE.delete_turns(
                remove_turn_ids)
        else:
            trace_rows_cleared = _NODE_TRACE_STORE.delete_case(case_id)
    sess.logger.log("rewind", {
        "message_id": msg_id, "case_id": case_id,
        "qa_cache_entries_cleared": n_cached,
        "kb_specialists_cleared": n_kb_specialists,
        "kb_kps_cleared": n_kb_total,
        "trace_rows_cleared": trace_rows_cleared,
        "aborted_in_flight_turn": aborted_in_flight,
        "task_cancel_dispatched": cancelled_task,
    })
    return ("", 204)


@app.get("/api/cases/<case_id>/charts/<path:filename>")
def get_chart(case_id: str, filename: str):
    """Serve a rendered chart PNG from `reports/<case_id>/charts/`.

    The agent_message markdown emitted by the run loop contains image
    references like `![topic](/api/cases/<case_id>/charts/<file>)`; the
    React app's existing markdown renderer then GETs this route.

    Path-traversal guard: ``send_from_directory`` already rejects paths
    that escape the directory, but we additionally pre-screen `..` and
    backslashes so a malformed request fails fast with a 404 (not a 500).
    """
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        abort(404)
    charts_dir = (_REPORTS_DIR / case_id / "charts").resolve()
    if not charts_dir.exists():
        abort(404)
    mimetype = "image/svg+xml" if filename.endswith(".svg") else "image/png"
    return send_from_directory(charts_dir, filename, mimetype=mimetype)


@app.get("/api/cases/<case_id>/stream")
def stream(case_id: str):
    try:
        sess = _get_or_create_session(case_id)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404

    sub_q: queue.Queue = queue.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
    with sess.subscribers_lock:
        sess.subscribers.append(sub_q)
        # Snapshot the replay buffer ATOMICALLY with registration: any event
        # emitted after this point goes to `sub_q` (delivered live below), so
        # the replay and the live stream never overlap. Recovers a turn that
        # ran while this client was disconnected (the `turn_no_subscribers`
        # hole) instead of losing it.
        replay_frames = list(sess.event_buffer)
    print(f"[SSE] +client case={case_id} (total: {len(sess.subscribers)}, "
          f"replay: {len(replay_frames)} buffered)")

    def _generate():
        # Initial open frame
        yield ": connected\n\n"
        # Replay recent events first so a (re)connecting client recovers a
        # turn that completed (or is mid-flight) while it was disconnected.
        # Idempotent on the client: trace events upsert by turn_id/call_id and
        # chat messages dedupe by id.
        for event_name, payload in replay_frames:
            yield f"event: {event_name}\ndata: {_sse_data(payload)}\n\n"
        last_ping = time.time()
        try:
            while True:
                try:
                    event_name, payload = sub_q.get(timeout=1.0)
                    yield f"event: {event_name}\ndata: {_sse_data(payload)}\n\n"
                except queue.Empty:
                    if time.time() - last_ping > PING_INTERVAL_S:
                        # VISIBLE heartbeat (named event, not a `:` comment).
                        # SSE comments keep the socket warm but are NOT
                        # delivered to JS listeners — so the frontend can't
                        # use them to detect a silently half-open connection.
                        # A named `ping` event reaches useSSE's staleness
                        # watchdog, which rebuilds a dead stream instead of
                        # stranding every later turn until a hard refresh.
                        yield "event: ping\ndata: {}\n\n"
                        last_ping = time.time()
        finally:
            with sess.subscribers_lock:
                if sub_q in sess.subscribers:
                    sess.subscribers.remove(sub_q)
            print(f"[SSE] -client case={case_id} (total: {len(sess.subscribers)})")

    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


def _start_trace_viewer() -> None:
    """Launch the node-trace viewer on a background thread.

    Shares the same NODE_TRACE_DB as the main server so traces appear
    in real time without a separate ``python -m tools.node_trace.viewer``
    process. Runs on port 3002 by default (override with TRACE_VIEWER_PORT).
    Disable with TRACE_VIEWER_DISABLE=1.
    """
    if os.environ.get("TRACE_VIEWER_DISABLE") == "1":
        return
    if _NODE_TRACE_STORE is None:
        return
    try:
        from tools.node_trace.viewer import app as viewer_app
        viewer_port = int(os.environ.get("TRACE_VIEWER_PORT", "3002"))
        viewer_app.config["NODE_TRACE_DB"] = _NODE_TRACE_DB_PATH
        import threading
        t = threading.Thread(
            target=viewer_app.run,
            kwargs=dict(host=HOST, port=viewer_port, debug=False),
            daemon=True,
        )
        t.start()
        print(f"[trace viewer] http://{HOST}:{viewer_port}  db={_NODE_TRACE_DB_PATH}")
    except Exception as exc:  # noqa: BLE001
        print(f"[trace viewer] failed to start: {exc}")


if __name__ == "__main__":
    _start_trace_viewer()
    print(f"[server] listening on http://{HOST}:{PORT}")
    # threaded=True so SSE streams + POST handlers don't block each other.
    # use_reloader=False because the bootstrap above is heavy and reloads cause double-init.
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)
