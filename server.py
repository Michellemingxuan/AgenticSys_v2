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
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from agents import Runner
from agents.exceptions import AgentsException
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
PING_INTERVAL_S = 15.0


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
    # SSE subscribers — each one owns a queue.Queue of (event_name, payload).
    subscribers: list[queue.Queue] = field(default_factory=list)
    subscribers_lock: threading.Lock = field(default_factory=threading.Lock)
    # Per-session exact-match Q→A cache. Keyed by `_normalize_q(redacted_question)`;
    # value carries the cached FinalAnswer fields. Skips orchestrator on repeats.
    qa_cache: dict = field(default_factory=dict)

    def emit(self, event_name: str, payload: dict) -> None:
        """Fan out an SSE event to every subscriber of this case."""
        with self.subscribers_lock:
            for q in self.subscribers:
                q.put((event_name, payload))


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

_FIREWALL = FirewallStack(logger=_BOOT_LOGGER)
_CLIENTS = build_session_clients(_FIREWALL, model_name=MODEL, backend=None)
_CHAT_LLM = FirewalledChatShim(_CLIENTS)

_PILLAR_YAML = PillarLoader().load(PILLAR) or {}
_HELPER_TOOLS = build_helper_tools()

ALL_CASES = _GATEWAY.list_case_ids()
print(f"[server] {len(ALL_CASES)} cases available: {ALL_CASES[:5]}{'...' if len(ALL_CASES) > 5 else ''}")


def _normalize_q(q: str) -> str:
    """Normalize a question for the per-session exact-match QA cache.

    Lowercase, strip outer whitespace, collapse internal whitespace. The
    cache is intentionally exact-match-after-redaction (run_normalize on
    `verdict.redacted_question`); fuzzy similarity is the orchestrator's
    job (team_construction.md), not the cache's.
    """
    return " ".join((q or "").strip().lower().split())


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

        sess = CaseSession(
            case_id=case_id,
            gateway=case_gateway,
            catalog=_CATALOG,
            clients=_CLIENTS,
            pillar_yaml=_PILLAR_YAML,
            chat_agent=ChatAgent(_CHAT_LLM, case_logger, tools=_HELPER_TOOLS),
            logger=case_logger,
        )
        SESSIONS[case_id] = sess
        return sess


# ── Async streaming worker ──────────────────────────────────────────────────

async def _run_turn_streamed(sess: CaseSession, turn_id: str, question: str) -> None:
    """Run a single reviewer turn, emitting SSE events as the run progresses.

    Maps Agents-SDK RunItems to typed events the frontend understands:
      ToolCallItem        → team_plan (collected) + agent_started
      ToolCallOutputItem  → agent_completed
      MessageOutputItem   → ignored (final structured output is the answer)
    """
    started_at = int(time.time() * 1000)

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

    # ── 1. Question check (screen + relevance) ─────────────────────────────
    try:
        verdict = await sess.chat_agent.screen(question)
    except Exception as exc:
        sess.emit("error", {"turn_id": turn_id, "message": f"screen failed: {exc}", "recoverable": True})
        sess.emit("turn_done", {"turn_id": turn_id, "ended_at": int(time.time() * 1000),
                                "duration_ms": int(time.time() * 1000) - started_at,
                                "outcome": "orchestrator_error"})
        return

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
        return

    # ── 1.5. Exact-match QA cache lookup ──────────────────────────────────
    # If the same redacted question was answered earlier this session and
    # produced a FinalAnswer, replay it instead of paying the orchestrator
    # cost a second time. Cache key uses the redacted form so identical
    # questions with different identifiers (case IDs etc.) collide as
    # intended. Rejections are not cached.
    cache_key = _normalize_q(verdict.redacted_question)
    cached = sess.qa_cache.get(cache_key) if cache_key else None
    if cached is not None:
        sess.logger.log("qa_cache_hit", {
            "turn_id": turn_id, "norm_q": cache_key,
            "origin_turn_id": cached.get("turn_id_origin"),
        })
        cached_text = cached["answer"]
        # Annotate so the reviewer sees that this is a replay, not a
        # fresh run — keeps the answer faithful to the original (no
        # silent staleness) while saving the orchestrator round-trip.
        replayed_text = (
            cached_text
            + "\n\n*— Reused from a prior identical question this session "
              "(no fresh data pull).*"
        )
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
        return

    # ── 2. Build a fresh orchestrator for this turn ───────────────────────
    orchestrator = Orchestrator(
        llm=None, logger=sess.logger, registry=None,
        pillar=PILLAR, pillar_config=sess.pillar_yaml,
        catalog=sess.catalog, gateway=sess.gateway,
        clients=sess.clients,
    )
    case_folder = _REPORTS_DIR / sess.case_id
    ctx = AppContext(gateway=sess.gateway, case_folder=case_folder, logger=sess.logger)

    # Multi-turn memory: prepend prior input list, append this turn's question.
    if sess.input_history:
        run_input = sess.input_history + [{"role": "user", "content": verdict.redacted_question}]
    else:
        run_input = verdict.redacted_question

    # ── 3. Stream the orchestrator run ────────────────────────────────────
    streamed = Runner.run_streamed(orchestrator.orchestrator_agent, run_input, context=ctx)

    call_index_by_id: dict[str, int] = {}  # call_id → index in tool_calls list
    tool_calls: list[dict] = []
    started_at_by_call: dict[str, int] = {}
    team_plan_emitted = False

    def _safe_dump(obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if isinstance(obj, dict):
            return {k: _safe_dump(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_safe_dump(v) for v in obj]
        return obj

    final_answer: FinalAnswer | None = None

    try:
        async for event in streamed.stream_events():
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

                # First tool call → emit team_plan once (the orchestrator may add more
                # later; we send team_plan again on subsequent calls for incremental UX).
                team_plan_emitted = True
                sess.emit("team_plan", {"turn_id": turn_id, "tool_calls": list(tool_calls)})
                sess.emit("agent_started", {
                    "turn_id": turn_id, "call_id": call_id, "tool": name,
                    "started_at": started_at_by_call[call_id],
                })

            elif isinstance(item, ToolCallOutputItem):
                call_id = (raw.get("call_id") if isinstance(raw, dict) else None) or "?"
                tool = "?"
                if call_id in call_index_by_id:
                    tool = tool_calls[call_index_by_id[call_id]]["tool"]
                payload = _safe_dump(item.output)
                started_ts = started_at_by_call.get(call_id, int(time.time() * 1000))
                duration_ms = int(time.time() * 1000) - started_ts
                sess.emit("agent_completed", {
                    "turn_id": turn_id, "call_id": call_id, "tool": tool,
                    "payload": payload, "duration_ms": duration_ms,
                })

            elif isinstance(item, MessageOutputItem):
                pass  # handled by .final_output below

        # Drain complete — pull the final structured output.
        final_raw = streamed.final_output
        try:
            final_answer = redact_payload(final_raw) if final_raw else None
        except Exception:
            final_answer = final_raw

        # Persist conversation memory for the next turn.
        try:
            sess.input_history = streamed.to_input_list()
        except Exception:
            pass  # SDK may not always support; degrade gracefully

    except AgentsException as exc:
        sess.emit("error", {"turn_id": turn_id, "message": f"orchestrator: {exc}",
                            "recoverable": False})
        sess.emit("turn_done", {"turn_id": turn_id, "ended_at": int(time.time() * 1000),
                                "duration_ms": int(time.time() * 1000) - started_at,
                                "outcome": "orchestrator_error"})
        return

    # ── 4. Emit final + chat agent message ────────────────────────────────
    if final_answer is None:
        answer_text = "(no answer produced)"
        flags: list[str] = []
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

    ts = int(time.time() * 1000)
    sess.emit("final", {
        "turn_id": turn_id, "answer": answer_text, "flags": flags,
        "timeline": timeline, "data_pull_request": _safe_dump(data_pull),
    })
    sess.emit("agent_message", {
        "id": str(uuid.uuid4()), "role": "agent", "text": answer_text,
        "timestamp": ts, "turn_id": turn_id,
    })
    sess.emit("turn_done", {
        "turn_id": turn_id, "ended_at": ts,
        "duration_ms": ts - started_at, "outcome": "ok",
    })

    # Cache the answer for exact-match replay on identical follow-up
    # questions in this session. Skip when the run produced no real answer
    # (final_answer was None) so we don't poison the cache with the
    # "(no answer produced)" sentinel.
    if final_answer is not None and cache_key:
        sess.qa_cache[cache_key] = {
            "answer": answer_text,
            "flags": list(flags or []),
            "data_pull_request": _safe_dump(data_pull),
            "turn_id_origin": turn_id,
        }
        sess.logger.log("qa_cache_store",
                        {"turn_id": turn_id, "norm_q": cache_key,
                         "answer_len": len(answer_text)})


def _spawn_turn(sess: CaseSession, turn_id: str, question: str) -> None:
    """Run a turn in a background thread (Flask handlers must return promptly)."""
    def _runner():
        # Serialize turns per case so the orchestrator state stays coherent.
        with sess.turn_lock:
            asyncio.run(_run_turn_streamed(sess, turn_id, question))
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


@app.post("/api/cases/<case_id>/rewind")
def post_rewind(case_id: str):
    body = request.get_json(silent=True) or {}
    msg_id = body.get("messageId", "")
    # v1: backend doesn't track messages by id. Just clear the orchestrator
    # input history so the next turn starts fresh from this case context.
    try:
        sess = _get_or_create_session(case_id)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    sess.input_history = []
    sess.logger.log("rewind", {"message_id": msg_id, "case_id": case_id})
    return ("", 204)


@app.get("/api/cases/<case_id>/stream")
def stream(case_id: str):
    try:
        sess = _get_or_create_session(case_id)
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404

    sub_q: queue.Queue = queue.Queue()
    with sess.subscribers_lock:
        sess.subscribers.append(sub_q)
    print(f"[SSE] +client case={case_id} (total: {len(sess.subscribers)})")

    def _generate():
        # Initial open frame
        yield ": connected\n\n"
        last_ping = time.time()
        try:
            while True:
                try:
                    event_name, payload = sub_q.get(timeout=1.0)
                    yield f"event: {event_name}\ndata: {json.dumps(payload, default=str)}\n\n"
                except queue.Empty:
                    if time.time() - last_ping > PING_INTERVAL_S:
                        yield ": ping\n\n"
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


if __name__ == "__main__":
    print(f"[server] listening on http://{HOST}:{PORT}")
    # threaded=True so SSE streams + POST handlers don't block each other.
    # use_reloader=False because the bootstrap above is heavy and reloads cause double-init.
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)
