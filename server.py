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
import atexit
import copy
import json
import math
import os
import queue
import shutil
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
#
# Do NOT pass `override=True`. The bug this block fixes is `.env` being
# ignored ENTIRELY, which the plain call already solves — `.env` still wins
# over nothing-set, which is every normal `python server.py` run. `override`
# additionally makes `.env` beat variables the PARENT PROCESS set, which
# silently breaks anything that configures the server through the
# environment: `NODE_TRACE_DB=… python server.py`, containers, and the
# AgenticEval harness (which injects PORT / NODE_TRACE_DB per target and
# then reads the trace DB it asked for — with override it read an empty
# file and reported every token/latency metric as missing).
try:
    from dotenv import load_dotenv as _load_dotenv, find_dotenv as _find_dotenv
    _load_dotenv(_find_dotenv())
except ImportError:
    pass

# Apply the tuning YAML (config/tuning.yaml) into os.environ BEFORE any module
# reads these knobs at import time (EPISODIC_TURNS, AMEM_CONSOLIDATE_EVERY_N,
# AMEM_ACTIVE_KP_THRESHOLD). setdefault semantics: inline env / .env still win.
from config.tuning_loader import apply_tuning as _apply_tuning
_apply_tuning()

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from flask_cors import CORS

from models.app_context import AppContext  # re-exported for tests (server.AppContext)
from agent_factories.chat_agent import ChatAgent
from agent_factories.helper_tools import build_helper_tools
from config.pillar_loader import PillarLoader
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway, normalize_case_id
from llm.factory import FirewalledChatShim, build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from tools.node_trace import NodeTrace, TURN_SCOPE, TurnScope
from main import _DATA_TABLES_DIR, _resolve_data_source
from memory import (AmemConfig, PurgeOutcome, build_amem_manager,
                    build_session_brief, delete_case_memory, delete_turns)
from models.types import FinalAnswer
from runner.identity import SERVER_RUN_ID, compose_conversation_id, resolve_user_id
from runner.config import (
    PILLAR,
    _NODE_TRACE_DB_PATH,
    _NODE_TRACE_STORE,
    _ORCH_PLAN_TIMEOUT_S,
    _PRIOR_QUESTIONS_FOR_SCREEN,
    _REPORTS_DIR,
    _SCREEN_TIMEOUT_S,
)
# `_TurnAborted` is defined in `runner.turn.conductor` (the planning-watchdog
# helpers moved there); `_spawn_turn._runner` catches it below when a
# phase-boundary checkpoint inside `TurnRunner.run()` propagates it. Safe to
# import at module level here — `conductor.py` no longer imports `server`
# (dependency is one-way: server -> runner), so there is no load cycle.
from runner.turn.conductor import _TurnAborted
from tools.data_tools import case_scope, init_tools
from tools.fs_tools import list_report_sections
from datalayer import pin_store


# ── Configuration ───────────────────────────────────────────────────────────

MODEL = os.environ.get("MODEL", "gpt-4.1")
DATA_SOURCE = os.environ.get("DATA_SOURCE", "auto")
PORT = int(os.environ.get("PORT", 49002))
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
    _qa_turn_seq: int = 0   # monotonic per-session sequence for episodic ordering
    # Per-specialist KNOWLEDGE BASE — survives across turns within this session.
    # Keyed by specialist name; each value is a chronological list of
    # KnowledgePoint dicts produced by the distiller agent after each
    # specialist run. Older entries are RETAINED for audit when a newer KP
    # supersedes them; the active set is "latest per topic" (filter happens in
    # agent_tool._format_kp_digest). Cleared by /rewind alongside
    # qa_cache so a session reset wipes everything.
    specialist_kps: dict = field(default_factory=dict)
    # Amem session identity (metadata only; reads stay case-scoped/cross-session).
    # Rotated on full rewind / clear-history so prior sessions become immutable.
    session_id: str = ""
    # turn_id of the in-flight turn, for cancel-turn Amem cleanup.
    current_turn_id: str | None = None
    # Deterministic conversation identity — restart-invisible node_trace grouping
    # key. Computed once at session open: compose(case_id, user_id, pillar_id).
    # Never minted, never rotated by rewind — same case+user+pillar always
    # resolves to the same conversation_id, across server restarts.
    conversation_id: str = ""
    user_id: str = ""
    pillar_id: str = ""

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
# Pristine (YAML-loaded) catalog profiles, snapshotted BEFORE any case sync
# mutates them. `_sync_case_catalog` reads _profiles back as its "canonical"
# base, so every per-session catalog copy starts from THIS snapshot — otherwise
# one case's alias / value-vocabulary drift compounds onto the next. See
# `_get_or_create_session`.
_CATALOG_PRISTINE_PROFILES = copy.deepcopy(_CATALOG._profiles)
_BOOT_LOGGER = EventLogger(session_id=f"server-{uuid.uuid4().hex[:8]}")
# Process-wide DEFAULT scope only. Every turn binds its own case-scoped gateway
# + catalog over the top (`_case_scope`), so these globals are what non-turn
# code (boot, notebooks, tests) sees — never what a running turn reads.
init_tools(_GATEWAY, _CATALOG, logger=_BOOT_LOGGER)

_FIREWALL = FirewallStack(
    logger=_BOOT_LOGGER,
      specialist_concurrency=12,
      orchestrator_concurrency=2
      )
_CLIENTS = build_session_clients(_FIREWALL, model_name=MODEL, backend=None)
_CHAT_LLM = FirewalledChatShim(_CLIENTS)

_AMEM_CFG = AmemConfig.from_env()
_AMEM = build_amem_manager(
    _AMEM_CFG,
    backend=(getattr(_CLIENTS, "backend", None) or os.environ.get("LLM_BACKEND", "openai")),
    logger=_BOOT_LOGGER,
    # Amem synthesizes case summaries with its OWN LLM call (we never pass
    # `content`), over this case's questions and answers. Hand it the
    # firewalled client so those calls are redacted and gated like every other
    # model call here, instead of going out through a plain AsyncOpenAI.
    client=getattr(_CLIENTS, "firewalled_client", None),
)
atexit.register(lambda: _AMEM.close())


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

    # Resolve the tiktoken encoder here, off the hot path. It downloads its
    # BPE file on first use, so where that host is unreachable the lookup
    # blocks until the socket times out. It used to happen inside the first
    # traced LLM round — twice per round, from a coroutine, pinning the event
    # loop and making a ~2s turn take ~30s. Now an environment without egress
    # pays one timeout at boot and reports it, rather than paying it forever.
    _tok_t0 = time.time()
    from tools.node_trace.pricing import prewarm_encoder
    _tok_ok = prewarm_encoder(MODEL)
    _tok_ms = int((time.time() - _tok_t0) * 1000)
    _BOOT_LOGGER.log("tokenizer_prewarm", {
        "model": MODEL, "available": _tok_ok, "duration_ms": _tok_ms,
    })
    if not _tok_ok:
        print(f"[server] tiktoken unavailable after {_tok_ms}ms — token counts "
              f"will be estimated from text length. This affects trace/cost "
              f"telemetry only, not answers. On an air-gapped host set "
              f"TIKTOKEN_LOAD_TIMEOUT_S=0 to skip this probe; to restore exact "
              f"counts, populate TIKTOKEN_CACHE_DIR "
              f"(see tools/tiktoken_cache_fetch.py).")
    elif _tok_ms > 2000:
        print(f"[server] tiktoken encoder took {_tok_ms}ms to load "
              f"(downloaded?) — consider setting TIKTOKEN_CACHE_DIR.")

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


    # (Each turn starts fresh — no accumulated conversation history.
    #  Follow-up context lives in specialist_kps + KP warmth hint.)


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


def _restore_session_state(sess, case_id: str) -> None:
    """Restore a case's cross-turn RAM (qa_cache, specialist_kps)
    from the latest durable snapshot, so a server restart is invisible.
    Best-effort; never raises."""
    if _NODE_TRACE_STORE is None:
        return
    try:
        snap = _NODE_TRACE_STORE.load_latest_snapshot(case_id)
    except Exception:
        snap = None
    if not snap:
        return
    try:
        sess.qa_cache = snap.get("qa_cache") or {}
        sess.specialist_kps = snap.get("specialist_kps") or {}
        sess._qa_turn_seq = max(
            (e.get("turn_seq", 0) for e in sess.qa_cache.values()
             if isinstance(e, dict)), default=0)
        sess.logger.log("session_restored", {
            "case_id": case_id,
            "qa_entries": len(sess.qa_cache),
            "kp_specialists": len(sess.specialist_kps)})
    except Exception:
        pass


def _get_or_create_session(case_id: str) -> CaseSession:
    """Lazily build a CaseSession for this case_id."""
    # HTTP callers are already canonical via `_canonicalize_case_id`; this
    # covers the rest (tests, prewarm, any internal caller) so SESSIONS can
    # never hold two entries — and two logs, two report dirs — for one case.
    case_id = normalize_case_id(case_id)
    with SESSIONS_LOCK:
        sess = SESSIONS.get(case_id)
        if sess is not None:
            return sess

        if case_id not in ALL_CASES:
            raise KeyError(f"unknown case_id: {case_id}")

        # This session's OWN gateway + catalog, so concurrent turns on different
        # cases cannot re-scope each other. Previously both were process-global
        # singletons re-pointed at the start of every turn (`_rescope_to_case`),
        # which serialized correctness on a lock that only covered ONE case:
        # case A mid-flight + case B starting = A's remaining queries silently
        # reading B's rows and B's schema.
        #
        # The gateway view SHARES the loaded table corpus (only `_current_case`
        # is per-instance), so this costs no data memory. The catalog must be a
        # real copy: `_sync_case_catalog` MUTATES `_profiles` with this case's
        # aliases and observed value vocabularies.
        #
        # `_profiles` is the only mutable state on a DataCatalog, so a shallow
        # copy plus a fresh deepcopy of the pristine profiles fully isolates it
        # (and re-reading the YAMLs per session would be far more expensive).
        case_gateway = _GATEWAY.for_case(case_id)
        case_catalog = copy.copy(_CATALOG)
        case_catalog._profiles = copy.deepcopy(_CATALOG_PRISTINE_PROFILES)

        case_logger = EventLogger(session_id=f"case-{case_id}-{uuid.uuid4().hex[:6]}")
        case_logger.log("case_session_open", {"case_id": case_id})

        # First-open: reconcile the canonical catalog against this case's
        # actual CSV columns so specialists' get_table_schema sees accurate
        # aliases + observed value vocabularies for THIS case. In-memory only.
        _sync_case_catalog(case_id, case_gateway, case_catalog, case_logger)

        sess = CaseSession(
            case_id=case_id,
            gateway=case_gateway,
            catalog=case_catalog,
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
        sess.session_id = case_logger.session_id
        # Attach the Amem handle to the session so the conductor reads it off
        # `sess` instead of `import server` (which would re-run this bootstrap
        # under a second module instance and rebind the data tools to an
        # unscoped gateway — see runner/turn/conductor._assemble_input).
        sess.amem = _AMEM
        sess.amem_cfg = _AMEM_CFG
        # Deterministic conversation identity — same (case, user, pillar) always
        # resolves to the same conversation_id, across server restarts. This is
        # what makes node_trace rows restart-invisible for a given case.
        sess.user_id = resolve_user_id(_AMEM_CFG)
        sess.pillar_id = PILLAR
        sess.conversation_id = compose_conversation_id(
            case_id, sess.user_id, sess.pillar_id)
        try:
            brief = build_session_brief(_AMEM, _AMEM_CFG, case_id=case_id)
            sess.emit("session_brief", {"case_id": case_id, "text": brief})
        except Exception:
            pass
        _restore_session_state(sess, case_id)
        SESSIONS[case_id] = sess
        return sess


# ── Plan-review phased-run helpers (Task 6) ─────────────────────────────────
#
# The six coherence-review helpers (multi-specialist gate, dispatch-count
# accessors, `_run_review`, `_invalidate_specialist_distillation`, and
# `_apply_review_directive`) plus `_AUX_REVIEW_TOOLS` live in
# `runner/turn/review.py` (extracted Task 3 of the turn-runner-pipeline
# refactor — see the imports above). The state-coupled `_run_redispatch_pass`
# now lives as a method on `runner.turn.conductor.TurnRunner` (Task 5) and is
# passed into `_apply_review_directive` as `run_redispatch_pass_fn`.


# ── Async streaming worker ──────────────────────────────────────────────────

def _case_scope(sess: CaseSession):
    """Bind THIS session's gateway + catalog for the whole turn.

    Replaces the old `_rescope_to_case`, which re-pointed process-global
    singletons at the start of every turn. That worked for turns run one after
    another, but nothing stopped two CASES from running at once: `turn_lock` is
    per-case, so case B's turn could re-point the globals while case A's turn
    was mid-flight, and A's remaining queries would silently read B's rows and
    B's schema. Not an error — plausible numbers for the wrong case.

    Now each session owns its gateway view and catalog copy (built in
    `_get_or_create_session`) and the binding is CONTEXT-local, so concurrent
    turns cannot see each other's. Must be entered inside the turn's own thread:
    a fresh thread starts with an empty context, so binding from the spawning
    thread would not reach it (`_spawn_turn` starts the thread, this runs in it).
    """
    sess.logger.log("case_scope_bound", {
        "case_id": sess.case_id,
        "gateway_case": sess.gateway.get_case_id(),
    })
    return case_scope(sess.gateway, sess.catalog)


async def _run_turn_streamed(
    sess: CaseSession, turn_id: str, question: str,
    started_at: int | None = None,
) -> None:
    """Run a single reviewer turn, emitting SSE events as the run progresses.

    Thin wrapper over ``runner.turn.conductor.TurnRunner`` — the runtime spine
    for one reviewer turn. The turn body (screen → cache-replay → assemble
    → orchestrator stream → coherence review + re-dispatch → finalize) lives
    there as phase methods; this wrapper just constructs and runs it so
    ``_spawn_turn``'s call site (and its wall-clock fence / cancellation) is
    unchanged. ``TurnRunner`` is imported lazily here (rather than at module
    top) simply to keep this heavy import off `server.py`'s import path until
    a turn actually runs; there is no module-load cycle — `conductor.py`
    does not import `server`.

    ``started_at`` is the ms-since-epoch timestamp from when the turn was
    received. ``_spawn_turn`` already emits the visible "new turn" events
    (reviewer_message, turn_started, empty team_plan) BEFORE acquiring the
    per-case turn lock so the frontend resets immediately even on lock
    contention; this function picks up after those have fired and uses
    ``started_at`` for duration math.
    """
    # Bind THIS case's data scope BEFORE any tool (screen included) runs, and
    # hold it for the whole turn so concurrent turns on other cases can't
    # re-point what this one reads.
    from runner.turn.conductor import TurnRunner
    with _case_scope(sess):
        await TurnRunner(sess, turn_id, question, started_at).run()


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
                sess.current_turn_id = turn_id
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
                sess.current_turn_id = None
        finally:
            sess.turn_lock.release()
    threading.Thread(target=_runner, daemon=True, name=f"turn-{turn_id[:8]}").start()


def _history_messages(qa_cache: dict,
                      turn_times: dict[str, float] | None = None) -> list[dict]:
    """Reconstruct the visible thread from qa_cache, ordered by turn_seq.
    Two messages per turn: the reviewer question then the agent answer.

    Each message carries `timestamp` in epoch MILLISECONDS, which is what the
    browser's `new Date(ts)` expects — Python's epoch seconds rendered
    straight would date every turn to 1970. Omitted entirely when no time is
    known, so the UI's "no timestamp -> render nothing" path is reached
    rather than a zero being formatted as a real clock time.

    Two sources, in order:

      1. `turn_times` from node_trace — the ACCURATE one. The trace opens its
         first node when the turn starts, so it is a true ask time, and its
         rows outlive the session, which is what lets a thread restored days
         later still carry real times.
      2. the entry's own `asked_at`, stamped by `_store_cached_qa`. Covers
         turns node_trace never saw (`NODE_TRACE_DISABLE=1`, or rows since
         purged). Written at turn completion when the caller passes no start,
         so it can run a turn-duration late.

    Before either existed the endpoint sent no time at all, and the UI showed
    a clock only for turns asked in that browser since the last history load
    — so the same thread had times on some rows and not others depending on
    nothing the reviewer could see.
    """
    entries = sorted(
        (e for e in (qa_cache or {}).values() if isinstance(e, dict)),
        key=lambda e: e.get("turn_seq", 0))
    out: list[dict] = []
    for e in entries:
        tid = e.get("turn_id_origin") or ""
        q = e.get("origin_question") or ""
        a = e.get("answer") or ""
        secs = (turn_times or {}).get(tid)
        if secs is None:
            secs = e.get("asked_at")
        stamp = {}
        if isinstance(secs, (int, float)) and secs > 0:
            stamp = {"timestamp": int(secs * 1000)}
        if q:
            out.append({"id": f"hist:{tid}:reviewer", "role": "reviewer",
                        "text": q, "turn_id": tid, **stamp})
        if a:
            out.append({"id": f"hist:{tid}:agent", "role": "agent",
                        "text": a, "turn_id": tid, **stamp})
    return out


# ── Flask app ───────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["http://localhost:59021", "http://127.0.0.1:59021"]}})


@app.url_value_preprocessor
def _canonicalize_case_id(endpoint, values):  # noqa: ARG001 — Flask signature
    """Normalize the `<case_id>` path segment for EVERY route, once.

    The HTTP path is one of the three doors a case id comes through (see
    `normalize_case_id`), and it is the one most likely to carry padding:
    the id is round-tripped through a URL, a bookmark, or a hand-edited
    address bar. Handlers then use it BOTH as the session key and to build
    paths — `_REPORTS_DIR / case_id / "charts"` in the rewind handlers, the
    log filename in `_get_or_create_session`. Normalizing per handler means
    every new route has to remember; doing it here means none of them can
    forget, and each handler's `case_id` argument is canonical by the time
    it runs.
    """
    if values and "case_id" in values:
        values["case_id"] = normalize_case_id(values["case_id"])


@app.get("/api/cases")
def get_cases():
    return jsonify(_split_cases(ALL_CASES))


@app.get("/api/pillars")
def get_pillars():
    """Available pillars, and which one this server is running.

    `active` is fixed at boot: `PILLAR` selects one config, which is then
    baked into `_PILLAR_YAML`, the chat agent's `pillar_config`, and every
    session's conversation identity. Switching at runtime would have to be a
    per-session change, so the UI shows the others as unavailable rather than
    offering a control that silently does nothing.
    """
    loader = PillarLoader()
    out = []
    for name in loader.list_pillars():
        cfg = loader.load(name) or {}
        out.append({
            "id": name,
            "display_name": cfg.get("display_name") or name,
            "focus": cfg.get("focus") or "",
        })
    return jsonify({"active": PILLAR, "pillars": out})


@app.get("/api/cases/overview")
def get_cases_overview():
    """Every case with the two dates a reviewer picks by.

    `report_updated_at` is the newest mtime across the case's curated report
    files; `last_qa_at` is when a question was last asked of it. Cases with
    neither have simply never been opened, and are listed anyway — the point
    of this page is to show what is waiting as much as what is done.
    """
    activity = (_NODE_TRACE_STORE.case_activity()
                if _NODE_TRACE_STORE is not None else {})
    rows = []
    for case_id in ALL_CASES:
        cid = str(case_id)
        folder = _REPORTS_DIR / cid
        sections = list_report_sections(folder) if folder.is_dir() else []
        stamps = [(folder / sec["filename"]).stat().st_mtime
                  for sec in sections if sec["filename"]]
        act = activity.get(cid, {})
        rows.append({
            "case_id": cid,
            "report_updated_at": (
                time.strftime("%Y-%m-%d", time.localtime(max(stamps))) if stamps else None),
            "report_sections": sum(1 for sec in sections if sec["markdown"]),
            "last_qa_at": act.get("last_qa_at"),
            "turns": act.get("turns", 0),
            "pins": len(pin_store.list_pins(cid)),
        })
    # Most recently reviewed first; never-opened cases sink to the bottom
    # rather than being hidden, since those are the ones needing attention.
    rows.sort(key=lambda r: (r["last_qa_at"] or "", r["report_updated_at"] or ""),
              reverse=True)
    return jsonify({"cases": rows})


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


@app.get("/api/cases/<case_id>/history")
def get_history(case_id: str):
    try:
        sess = _get_or_create_session(case_id)   # triggers restore
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    # node_trace is the accurate clock for turns it recorded, including ones
    # from sessions long gone; the qa_cache stamp covers the rest.
    turn_times = (_NODE_TRACE_STORE.turn_started_at(case_id)
                  if _NODE_TRACE_STORE is not None else {})
    return jsonify({"messages": _history_messages(sess.qa_cache, turn_times)})


@app.post("/api/cases/<case_id>/cancel-turn")
def post_cancel_turn(case_id: str):
    """Interrupt the in-flight turn AND fully rewind — as if the
    stopped question was never asked.

    Clears: specialist_kps (current turn's KPs) and qa_cache (so re-asking
    doesn't replay). Cancels the running task via
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
    sess._qa_turn_seq = 0   # episodic ordering counter rewinds with qa_cache (spec §4)
    n_kp_total = sum(len(v) for v in sess.specialist_kps.values())
    sess.specialist_kps.clear()
    with sess.subscribers_lock:
        sess.event_buffer.clear()  # don't replay events from the wiped turn(s)

    amem_purge = PurgeOutcome(skipped=True)
    if sess.current_turn_id:
        amem_purge = delete_turns(_AMEM, _AMEM_CFG, case_id=case_id,
                                  turn_ids=[sess.current_turn_id],
                                  logger=sess.logger)

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
        "kp_entries_cleared": n_kp_total,
        "charts_cleared": n_charts_cleared,
        **amem_purge.as_log_fields(),
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
        # Drop KP entries produced during the removed turns.
        n_kp_total = 0
        for spec_name in list(sess.specialist_kps):
            before = len(sess.specialist_kps[spec_name])
            sess.specialist_kps[spec_name] = [
                kp for kp in sess.specialist_kps[spec_name]
                if kp.get("captured_at_turn") not in removed_set
            ]
            n_kp_total += before - len(sess.specialist_kps[spec_name])
        n_kp_specialists = sum(
            1 for kps in sess.specialist_kps.values() if kps
        )
        amem_purge = delete_turns(_AMEM, _AMEM_CFG, case_id=case_id,
                                  turn_ids=remove_turn_ids, logger=sess.logger)
        if _NODE_TRACE_STORE is not None:
            try:
                _max_seq = max((e.get("turn_seq", 0) for e in sess.qa_cache.values()
                                if isinstance(e, dict)), default=0)
                _NODE_TRACE_STORE.snapshot_session(
                    chat_id=sess.conversation_id, case_id=case_id,
                    turn_id=f"rewind-{_max_seq}",
                    qa_cache=sess.qa_cache, specialist_kps=sess.specialist_kps,
                    conversation_id=sess.conversation_id,
                    server_run_id=SERVER_RUN_ID, user_id=sess.user_id,
                    pillar_id=sess.pillar_id)
            except Exception:
                pass
    else:
        # Full rewind: clear everything.
        n_cached = len(sess.qa_cache)
        sess.qa_cache.clear()
        sess._qa_turn_seq = 0   # episodic ordering counter rewinds with qa_cache (spec §4)
        n_kp_specialists = len(sess.specialist_kps)
        n_kp_total = sum(len(v) for v in sess.specialist_kps.values())
        sess.specialist_kps.clear()
        sess.session_id = f"case-{case_id}-{uuid.uuid4().hex[:6]}"

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
    if not is_partial:
        amem_purge = delete_case_memory(_AMEM, _AMEM_CFG, case_id=case_id,
                                        logger=sess.logger)

    # Pins are treated differently by the two modes, on purpose.
    #
    # A PARTIAL rewind retracts: the pin is kept but stops claiming to be
    # current, and is lifted out of any report section. Pinning is a deliberate
    # "this matters" while rewind is one click on a turn card, so deleting here
    # would destroy filed work as a side effect of a casual action — and the
    # usual reason to rewind is to re-ask a question better, not to disown the
    # finding. What it MUST NOT do is leave the pin looking current: turn
    # numbers are positional, so dropping an earlier turn renumbers the rest
    # and a pin captured as "Turn 3" would point at a different turn.
    #
    # A FULL clear deletes. "Clear this case" is an explicit reset of
    # everything, and finding the pins still there would be the surprise.
    if is_partial:
        pins_affected = pin_store.retract_turns(case_id, remove_turn_ids)
    else:
        pins_affected = pin_store.delete_all_pins(case_id)
    # The `rewind` event is THE record of what a clear actually did, so it
    # carries the Amem outcome alongside the caches it already reported. Without
    # these fields a purge that never ran (store unreachable) was indistinguish-
    # able from one that emptied the case — the user saw 204, a wiped UI, and
    # memory that was still there.
    sess.logger.log("rewind", {
        "message_id": msg_id, "case_id": case_id,
        "qa_cache_entries_cleared": n_cached,
        "kp_specialists_cleared": n_kp_specialists,
        "kp_kps_cleared": n_kp_total,
        "trace_rows_cleared": trace_rows_cleared,
        "aborted_in_flight_turn": aborted_in_flight,
        "task_cancel_dispatched": cancelled_task,
        "pins_retracted" if is_partial else "pins_deleted": pins_affected,
        **amem_purge.as_log_fields(),
    })
    return ("", 204)


@app.get("/api/cases/<case_id>/report")
def get_report(case_id: str):
    """Serve the curated case report — the same `reports/<case_id>/*.md`
    files the report agent reads — for the UI's Case Report panel.

    Returns every known section in reviewer-facing order, each with its
    markdown inline. Inline rather than one request per section because the
    whole report is ~75KB for a real case: a single response costs less than
    ten round trips and spares the panel a loading state on every tab click.

    Sections with no file on disk come back with `markdown: null` and are
    rendered as unavailable — a case missing its Bureau report should say so
    rather than quietly show nine tabs.

    Reads through `tools.fs_tools` on purpose. That module's `is_report_file`
    is the single definition of "a report file", and its docstring records
    what happened last time the definition was copied instead of imported
    (a `.MD` report that detection saw and the file list did not). This
    endpoint is the fourth consumer, not a fourth literal.
    """
    folder = (_REPORTS_DIR / case_id).resolve()
    try:
        folder.relative_to(_REPORTS_DIR.resolve())
    except ValueError:
        abort(404)
    if not folder.is_dir():
        return jsonify({"error": f"no report folder for case {case_id}"}), 404

    sections = list_report_sections(folder)
    # Newest mtime across the section files actually present. Deliberately
    # NOT called "generated": the reports carry no generation stamp of their
    # own, and mtime is a poor proxy for one — a Drive/rsync copy rewrites it,
    # so these files currently date to when they synced, not to when the
    # report was produced. The UI labels it "Updated" for that reason. Give
    # the reports a real stamp and this should read it instead.
    #
    # Taken per-file rather than from the folder: the folder's mtime moves
    # whenever `charts/` is written during a turn, which would date the
    # report to the last question asked.
    stamps = [(folder / sec["filename"]).stat().st_mtime
              for sec in sections if sec["filename"]]
    updated_at = (
        time.strftime("%Y-%m-%d", time.localtime(max(stamps))) if stamps else None
    )
    # Merge in any figures the reviewer inserted into a section. Attached as
    # a `figures` list rather than spliced into the markdown: the markdown is
    # the report agent's source text, and rewriting it would change what the
    # agent reads on the next turn. The panel renders these below the prose.
    by_section = pin_store.pins_by_section(case_id)
    for sec in sections:
        sec["figures"] = by_section.get(sec["key"], [])

    return jsonify({
        "case_id": case_id,
        "updated_at": updated_at,
        "sections": sections,
    })


def _snapshot_pinned_chart(case_id: str, chart_url: str | None) -> str | None:
    """Copy a turn's chart into the case's durable `pins/` dir.

    A pin is a review DELIVERABLE; the chart it points at is a per-turn
    artifact that `rewind` deletes. Pinning a figure and then rewinding its
    turn therefore left the pin alive with a 404 behind it — the report
    rendered the browser's broken-image glyph, which reads as a corrupted
    report rather than as retracted evidence.

    Snapshotting at pin time makes the pin self-contained. Keyed by the source
    FILENAME rather than the pin id, so two pins on the same chart share one
    copy and re-pinning is idempotent.

    Returns the durable URL, or the original one unchanged when there is
    nothing to copy (already a snapshot, or the source is already gone) —
    never raises, because failing to snapshot must not fail the pin.
    """
    if not chart_url:
        return chart_url
    marker = f"/api/cases/{case_id}/charts/"
    if marker not in chart_url:
        return chart_url          # already durable, or not ours to copy
    filename = chart_url.rsplit("/", 1)[-1]
    if not filename or "/" in filename or ".." in filename:
        return chart_url
    src = (_REPORTS_DIR / case_id / "charts" / filename).resolve()
    dst_dir = (_REPORTS_DIR / case_id / "pins").resolve()
    try:
        src.relative_to((_REPORTS_DIR / case_id / "charts").resolve())
    except ValueError:
        return chart_url
    if not src.is_file():
        return chart_url          # nothing to snapshot; the guard in the UI copes
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / filename
        if not dst.exists():
            shutil.copy2(src, dst)
    except OSError as exc:
        print(f"[server] pin snapshot failed for {filename}: {exc}")
        return chart_url
    return f"/api/cases/{case_id}/pinned-figures/{filename}"


@app.get("/api/cases/<case_id>/pinned-figures/<path:filename>")
def get_pinned_figure(case_id: str, filename: str):
    """Serve a snapshotted pin figure from `reports/<case_id>/pins/`.

    Separate from `/charts/` on purpose: these outlive the turn that drew
    them, which is the whole point of the snapshot.
    """
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        abort(404)
    pins_dir = (_REPORTS_DIR / case_id / "pins").resolve()
    if not pins_dir.exists():
        abort(404)
    mimetype = "image/svg+xml" if filename.endswith(".svg") else "image/png"
    return send_from_directory(pins_dir, filename, mimetype=mimetype)


@app.get("/api/cases/<case_id>/pins")
def get_pins(case_id: str):
    """Every pin on this case — insights and figures, oldest first."""
    return jsonify({"pins": pin_store.list_pins(case_id)})


@app.post("/api/cases/<case_id>/pins")
def post_pin(case_id: str):
    """Pin an insight or a figure.

    Figure pins are idempotent on (turn, specialist, topic) in the store, so
    the design's "Pin Figures" button — which pins a whole turn's figures at
    once — can be clicked twice without duplicating cards.
    """
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip()
    try:
        pin = pin_store.add_pin(
            case_id,
            kind=kind,
            text=(body.get("text") or "").strip(),
            turn_id=body.get("turn_id"),
            turn_index=body.get("turn_index"),
            source=(body.get("source") or "").strip(),
            specialist=body.get("specialist"),
            topic=body.get("topic"),
            # Snapshot so the pin survives a rewind of its source turn.
            chart_url=_snapshot_pinned_chart(case_id, body.get("chart_url")),
            # The spec is the durable copy: self-contained JSON that outlives
            # the PNG. The snapshot above is the fallback, not the other way
            # round.
            vega_spec=body.get("vega_spec"),
            chart_kind=body.get("chart_kind"),
            # Stable provenance. `turn_index` is positional and renumbers on
            # rewind, so the question text is what actually identifies a pin.
            question=(body.get("question") or "").strip() or None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(pin), 201


@app.delete("/api/cases/<case_id>/pins/<pin_id>")
def delete_pin(case_id: str, pin_id: str):
    if not pin_store.delete_pin(case_id, pin_id):
        return jsonify({"error": "no such pin"}), 404
    return ("", 204)


@app.delete("/api/cases/<case_id>/pins/retracted")
def delete_retracted_pins(case_id: str):
    """Remove every retracted pin for a case.

    Backs the board's one-click cleanup. Retraction keeps a rewound pin
    visible-but-marked rather than deleting it; this is where the reviewer
    chooses to let it go.
    """
    return jsonify({"deleted": pin_store.delete_retracted(case_id)})


@app.post("/api/cases/<case_id>/pins/<pin_id>/section")
def put_pin_section(case_id: str, pin_id: str):
    """Insert a pin into a report section, or lift it back out.

    `section_key` null removes the association. The key is validated against
    the report's own sections so a typo cannot strand a pin in a section that
    will never render.
    """
    body = request.get_json(silent=True) or {}
    section_key = body.get("section_key")
    if section_key is not None:
        folder = (_REPORTS_DIR / case_id).resolve()
        valid = {sec["key"] for sec in list_report_sections(folder)}
        if section_key not in valid:
            return jsonify({"error": f"unknown section {section_key!r}"}), 400
    if not pin_store.set_pin_section(case_id, pin_id, section_key):
        return jsonify({"error": "no such pin"}), 404
    return ("", 204)


@app.post("/api/cases/<case_id>/synthesis")
def post_synthesis(case_id: str):
    """Synthesise the reviewer's selected pins.

    `mode` is `story` (what these pins say read together) or `opportunities`
    (what follow-ups they justify). Both return the same shape so the board
    renders one component either way.

    Runs the agent synchronously on its own event loop. That is fine here in
    a way it would not be for a turn: this is one tool-less round with a
    1200-token cap, not a multi-specialist fan-out, and the reviewer is
    sitting on the button waiting for it.
    """
    body = request.get_json(silent=True) or {}
    mode = (body.get("mode") or "story").strip()
    if mode not in ("story", "opportunities"):
        return jsonify({"error": f"unknown mode {mode!r}"}), 400

    pin_ids = body.get("pin_ids") or []
    if not pin_ids:
        return jsonify({"error": "no pins selected"}), 400

    # Resolve server-side rather than trusting posted pin bodies: the client
    # may hold a stale copy, and a synthesis must be over what is actually
    # pinned right now.
    by_id = {p["pin_id"]: p for p in pin_store.list_pins(case_id)}
    pins = [by_id[i] for i in pin_ids if i in by_id]
    if not pins:
        return jsonify({"error": "none of those pins exist on this case"}), 404

    from agent_factories.synthesis_agent import build_synthesis_agent, render_pins
    from agents import Runner

    agent = build_synthesis_agent(_CLIENTS.model)
    prompt = render_pins(pins, mode)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            asyncio.wait_for(Runner.run(agent, prompt), timeout=90.0))
    except asyncio.TimeoutError:
        return jsonify({"error": "synthesis timed out"}), 504
    except Exception as exc:  # noqa: BLE001
        print(f"[server] synthesis failed: {type(exc).__name__}: {exc}")
        return jsonify({"error": f"synthesis failed: {type(exc).__name__}"}), 502
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

    out = result.final_output
    return jsonify({
        "mode": mode,
        "story": getattr(out, "story", "") or "",
        "opportunities": [
            {"title": o.title, "rationale": o.rationale}
            for o in (getattr(out, "opportunities", None) or [])
        ],
        "not_settled": list(getattr(out, "not_settled", None) or []),
        "pin_ids": [p["pin_id"] for p in pins],
    })


@app.get("/api/cases/<case_id>/opportunities")
def get_opportunities(case_id: str):
    return jsonify({"opportunities": pin_store.list_opportunities(case_id)})


@app.post("/api/cases/<case_id>/opportunities")
def post_opportunity(case_id: str):
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "missing title"}), 400
    opp = pin_store.add_opportunity(
        case_id,
        title=title,
        body=(body.get("body") or "").strip(),
        pin_ids=body.get("pin_ids") or [],
    )
    return jsonify(opp), 201


@app.delete("/api/cases/<case_id>/opportunities/<opp_id>")
def delete_opportunity(case_id: str, opp_id: str):
    if not pin_store.delete_opportunity(case_id, opp_id):
        return jsonify({"error": "no such opportunity"}), 404
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
    process. Runs on port 49003 by default (override with TRACE_VIEWER_PORT).
    Disable with TRACE_VIEWER_DISABLE=1.
    """
    if os.environ.get("TRACE_VIEWER_DISABLE") == "1":
        return
    if _NODE_TRACE_STORE is None:
        return
    try:
        from tools.node_trace.viewer import app as viewer_app
        viewer_port = int(os.environ.get("TRACE_VIEWER_PORT", "49003"))
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


# ── Built frontend ──────────────────────────────────────────────────────────
# Serving the SPA from Flask rather than from Vite, because the deployment
# target cannot run Vite: it has Node 10, and Vite 6 needs 18+. `npm run dev`
# dies on `import {` before it starts. Shipping the built `dist/` instead
# means the server needs no Node at all.
#
# It also removes a discrepancy rather than working around it. `BASE = '/api'`
# in the frontend is relative, so something must route it; in dev that is
# `server.proxy` in vite.config.ts, which does NOT apply to `vite preview`
# (that reads `preview.proxy`, unset here). Served from Flask the app is
# same-origin, so no proxy is involved on any path — and the CORS allowance
# stops mattering too.
#
# Build on a machine with a modern Node (`npm run build`) and ship `dist/`;
# it is gitignored, so add it to the archive deliberately.
_FRONTEND_DIST = Path(
    os.environ.get("FRONTEND_DIST")
    or (Path(__file__).resolve().parent.parent / "CaseReviewChat" / "dist")
).expanduser()


def _frontend_file(rel: str):
    """Send `rel` from the build, or None when it isn't a real file there.

    Returns a sentinel rather than raising, because the caller has to tell
    "no such asset" from "unknown route" to decide about the SPA fallback —
    which is why `send_from_directory` cannot simply be called directly.

    Containment is verified HERE, on the resolved path, rather than trusting
    that `..` was normalised out of the URL upstream: `Path(root) / "../x"`
    happily resolves outside the build, and `.is_file()` would say yes.
    Werkzeug does normalise, and `send_from_directory` has its own guard, but
    neither is local to this join and both could change.
    """
    root = _FRONTEND_DIST.resolve()
    try:
        candidate = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_file() or root not in candidate.parents:
        return None
    return send_from_directory(root, candidate.relative_to(root))


@app.get("/")
def serve_index():
    if not (_FRONTEND_DIST / "index.html").is_file():
        # An explicit message beats Flask's bare 404: the usual cause is a
        # deployment that shipped the source tree without running the build.
        return (f"No frontend build at {_FRONTEND_DIST}. Run `npm run build` "
                f"on a machine with Node 18+ and ship dist/, or point "
                f"FRONTEND_DIST at it.", 404)
    return send_from_directory(_FRONTEND_DIST, "index.html")


@app.get("/<path:filename>")
def serve_frontend(filename: str):
    """Static assets, with an index.html fallback for unknown paths.

    Registered last and guarded: `/api/...` rules are more specific so
    Werkzeug prefers them anyway, but an explicit reject means a typo'd API
    path returns 404 instead of silently serving the SPA — which would look
    like a broken page rather than a wrong URL.
    """
    if filename.startswith("api/"):
        abort(404)
    hit = _frontend_file(filename)
    if hit is not None:
        return hit
    # Unknown non-asset path: hand back the app. Harmless today (the UI keeps
    # its state in query params, not the path) and correct if it ever routes.
    if (_FRONTEND_DIST / "index.html").is_file():
        return send_from_directory(_FRONTEND_DIST, "index.html")
    abort(404)


if __name__ == "__main__":
    _start_trace_viewer()
    if (_FRONTEND_DIST / "index.html").is_file():
        print(f"[server] frontend: {_FRONTEND_DIST}")
    else:
        print(f"[server] frontend: NOT FOUND at {_FRONTEND_DIST} "
              f"(API still served; set FRONTEND_DIST or run `npm run build`)")
    print(f"[server] listening on http://{HOST}:{PORT}")
    # threaded=True so SSE streams + POST handlers don't block each other.
    # use_reloader=False because the bootstrap above is heavy and reloads cause double-init.
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)
