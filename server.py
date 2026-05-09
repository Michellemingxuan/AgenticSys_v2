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


# Keep this many of the most recent reviewer turns intact in input_history.
# Older turns get their tool-result payloads (the heavy SpecialistOutput JSON)
# replaced by a small stub. The orchestrator still sees that the call happened
# (call_id + tool name preserved), it just can't replay the raw findings from
# the elided turn — by design, since those findings now live in the
# specialists' KB and surface there on demand.
_INPUT_HISTORY_KEEP_RECENT_TURNS = 2

# Stub used to replace elided tool-result payloads. Kept terse so the
# orchestrator doesn't waste tokens parsing it; the specialist KB digest
# (passed into each specialist call) is the authoritative replay path.
_ELIDED_TOOL_OUTPUT = (
    "(elided — earlier-turn specialist output; see the specialist's KB "
    "digest, which is prepended to each new sub-question.)"
)


def _prune_input_history(history: list, keep_recent_turns: int) -> tuple[list, dict]:
    """Replace tool-result content in old turns with a small stub. Returns
    (pruned_history, stats).

    A "turn" is bounded by user messages: each `{"role": "user", ...}` entry
    starts a new turn. We keep the last ``keep_recent_turns`` turns intact;
    in older turns, any item that looks like a function_call_output has its
    output content replaced by ``_ELIDED_TOOL_OUTPUT``. Function-call items
    themselves (the call records) are preserved so the orchestrator's view
    of "what tools were invoked" stays accurate.

    Defensive: unknown item shapes are passed through unchanged. Returning
    the input list untouched on any structural surprise is safer than
    accidentally dropping content.
    """
    stats = {"items_total": len(history), "items_elided": 0, "bytes_saved": 0}
    if not isinstance(history, list) or not history:
        return history, stats

    # Find user-message indices to identify turn boundaries.
    user_idxs = [
        i for i, item in enumerate(history)
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    if len(user_idxs) <= keep_recent_turns:
        return history, stats  # All turns are recent — nothing to prune.

    cutoff_idx = user_idxs[-keep_recent_turns]
    pruned: list = []
    for i, item in enumerate(history):
        if i >= cutoff_idx:
            pruned.append(item)
            continue
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            old_output = item.get("output", "")
            if isinstance(old_output, str) and old_output != _ELIDED_TOOL_OUTPUT:
                stub = dict(item)
                stub["output"] = _ELIDED_TOOL_OUTPUT
                pruned.append(stub)
                stats["items_elided"] += 1
                stats["bytes_saved"] += max(0, len(old_output) - len(_ELIDED_TOOL_OUTPUT))
                continue
        pruned.append(item)
    return pruned, stats


def _normalize_q(q: str) -> str:
    """Normalize a question for the per-session exact-match QA cache.

    Lowercase, strip outer whitespace, collapse internal whitespace. The
    cache is intentionally exact-match-after-redaction (run_normalize on
    `verdict.redacted_question`); fuzzy similarity is the orchestrator's
    job (team_construction.md), not the cache's.
    """
    return " ".join((q or "").strip().lower().split())


# ── Phase 2 / 3 helpers — viz embedding + KB warmth ────────────────────────


def _format_kb_warmth_hint(specialist_kb: dict) -> str:
    """Build the one-line `[KB-warmth: …]` preface the orchestrator sees on
    every turn after the first one.

    Lists each specialist with non-empty KB and how many KPs it carries —
    the orchestrator uses this as a routing signal under `team_construction`'s
    follow-up rule (reuse warm specialists for in-domain follow-ups).

    Returns "" when no specialist has any KPs (e.g. first turn). The
    orchestrator never sees an empty hint — keeps prompts uncluttered when
    there's nothing to convey.
    """
    if not isinstance(specialist_kb, dict) or not specialist_kb:
        return ""
    warm = [(name, len(kps)) for name, kps in specialist_kb.items() if kps]
    if not warm:
        return ""
    warm.sort(key=lambda x: -x[1])
    parts = ", ".join(f"{name} ({n} KP{'s' if n != 1 else ''})" for name, n in warm)
    return (
        f"[KB-warmth: {parts}. "
        f"Strongly consider reusing warm specialists for in-domain follow-ups.]"
    )


def _collect_turn_charts(specialist_kb: dict, turn_id: str, case_id: str) -> list[dict]:
    """Find every KP captured in this turn that has a rendered chart.

    Returns a list of `{topic, url, specialist}` ready for embedding in
    the agent_message markdown. The URL points at the Flask route
    `/api/cases/<case_id>/charts/<filename>` so the frontend's existing
    markdown renderer fetches the PNG via a normal HTTP GET.

    Deduped by ``(specialist, topic)`` — when both the `make_chart` tool
    and the auto-distiller produce a chart for the same topic in one turn
    (the specialist explicitly charts a finding the distiller would have
    auto-charted anyway), only the latest one renders. Latest wins so the
    distiller's revision can correct an earlier explicit chart if needed.
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
            if not img_path:
                continue
            topic = kp.get("topic", "chart")
            filename = Path(img_path).name
            url = f"/api/cases/{case_id}/charts/{filename}"
            # Latest wins per (specialist, topic). Iteration order over
            # the KB's chronological list means the last appended entry
            # naturally overwrites the earlier one for the same key.
            by_key[(spec_name, topic)] = {
                "topic": topic,
                "url": url,
                "specialist": spec_name,
            }
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

    # ── 1. Question check (screen + relevance) ─────────────────────────────
    # Build the list of prior reviewer questions in this session so the
    # relevance_check skill can flag near-duplicates (matched on subject +
    # time-range + scope). The qa_cache holds raw redacted-question strings
    # as values' "origin_question"; we surface those here.
    prior_questions = [v.get("origin_question", "") for v in sess.qa_cache.values()]
    prior_questions = [q for q in prior_questions if q]
    screen_t0 = time.time()
    try:
        verdict = await sess.chat_agent.screen(question, prior_questions=prior_questions)
    except Exception as exc:
        sess.emit("error", {"turn_id": turn_id, "message": f"screen failed: {exc}", "recoverable": True})
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

    # ── 1.5. Cache lookup — exact-match first, then near-duplicate ───────
    # Cache key uses the redacted-question normalized form so identical
    # questions with different identifiers (case IDs etc.) collide as
    # intended. Rejections are not cached.
    cache_key = _normalize_q(verdict.redacted_question)
    cached = sess.qa_cache.get(cache_key) if cache_key else None
    cache_hit_kind = "exact" if cached is not None else None
    # Fall back to relevance_check's near-duplicate verdict — the LLM
    # judged this question a near-duplicate of an earlier one along
    # subject + time-range + scope dimensions. Look up that prior
    # question's cached answer.
    if cached is None and verdict.near_duplicate_of:
        near_dup_key = _normalize_q(verdict.near_duplicate_of)
        cached = sess.qa_cache.get(near_dup_key) if near_dup_key else None
        if cached is not None:
            cache_hit_kind = "near_duplicate"
            sess.logger.log("qa_cache_hit_near_duplicate", {
                "turn_id": turn_id,
                "matched_prior": verdict.near_duplicate_of,
                "match_reason": verdict.near_duplicate_reason,
            })
    if cached is not None:
        sess.logger.log("qa_cache_hit", {
            "turn_id": turn_id, "norm_q": cache_key,
            "origin_turn_id": cached.get("turn_id_origin"),
            "kind": cache_hit_kind,
        })
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
        return

    # ── 2. Build a fresh orchestrator for this turn ───────────────────────
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
    ctx = AppContext(
        gateway=sess.gateway,
        case_folder=case_folder,
        logger=sess.logger,
        _specialist_kb=sess.specialist_kb,
        _distiller=getattr(orchestrator, "distiller_agent", None),
        _turn_id=turn_id,
    )

    # Phase 3 — KB-warmth signal. When specialists have accumulated KPs from
    # earlier turns, prepend a one-line hint to the user question so the
    # orchestrator's team_construction step has a runtime signal that nudges
    # toward reusing warm specialists on in-domain follow-ups. The hint is
    # informational only — the orchestrator retains LLM judgment.
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

    # Multi-turn memory: prepend prior input list, append this turn's question.
    if sess.input_history:
        run_input = sess.input_history + [{"role": "user", "content": framed_question}]
    else:
        run_input = framed_question

    # ── 3. Stream the orchestrator run ────────────────────────────────────
    # Mark when we hand off to the orchestrator so we can measure the gap
    # to the first tool call. That gap = the orchestrator's first LLM call
    # (the team-construction decision); when the user reports "slow to
    # arrive at team construction", THIS is the number to look at.
    orch_t0 = time.time()
    sess.logger.log("turn_phase_orchestrator_starting", {
        "turn_id": turn_id,
        "input_history_len": len(sess.input_history),
        "input_history_chars": sum(
            len(json.dumps(item, default=str)) for item in sess.input_history
        ) if sess.input_history else 0,
        "warmth_hint_present": bool(warmth_hint),
        "n_specialists_warm": sum(1 for kps in sess.specialist_kb.values() if kps),
    })
    streamed = Runner.run_streamed(orchestrator.orchestrator_agent, run_input, context=ctx)

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
            sess.emit("error", {
                "turn_id": turn_id,
                "specialist": err.get("specialist"),
                "error_type": err.get("error_type"),
                "message": (
                    f"{err.get('specialist')}: "
                    f"{err.get('error_type')}: {err.get('error_message')}"
                ),
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
                call_id = (raw.get("call_id") if isinstance(raw, dict) else None) or "?"
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

            elif isinstance(item, MessageOutputItem):
                pass  # handled by .final_output below

        # Drain complete — pull the final structured output.
        final_raw = streamed.final_output
        try:
            final_answer = redact_payload(final_raw) if final_raw else None
        except Exception:
            final_answer = final_raw

        # Persist conversation memory for the next turn. Prune older turns'
        # tool-result payloads to keep input_history bounded — without this,
        # each turn's full SpecialistOutput JSON accumulates and feeds back
        # into every subsequent orchestrator call, dominating latency by
        # turn 5+. The specialists' KB (populated by the distiller) is the
        # replay path for elided content.
        try:
            raw_history = streamed.to_input_list()
            pruned, prune_stats = _prune_input_history(
                raw_history, keep_recent_turns=_INPUT_HISTORY_KEEP_RECENT_TURNS,
            )
            sess.input_history = pruned
            if prune_stats["items_elided"]:
                sess.logger.log("input_history_pruned", {
                    "turn_id": turn_id,
                    **prune_stats,
                    "kept_recent_turns": _INPUT_HISTORY_KEEP_RECENT_TURNS,
                    "history_len_after": len(pruned),
                })
        except Exception:
            pass  # SDK may not always support; degrade gracefully

    except AgentsException as exc:
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
        sess.emit("error", {
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
        return

    # Drain any errors that landed after the last tool call (e.g., a parallel
    # specialist that recorded its failure between the final agent_completed
    # and stream end).
    _drain_specialist_errors()

    # ── 4. Emit final + chat agent message ────────────────────────────────
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
        violation_flag = (
            f"general_specialist not invoked (protocol violation: "
            f"{len(unique_domain_specialists)} domain specialists ran without "
            f"the required cross-domain review)"
        )
        flags = list(flags or []) + [violation_flag]
        sess.logger.log("orchestrator_protocol_violation", {
            "turn_id": turn_id,
            "violation": "missing_general_specialist",
            "n_domain_specialists": len(unique_domain_specialists),
            "domain_specialists": sorted(unique_domain_specialists),
        })

    # Drain any in-flight distiller tasks before reading the KB for chart
    # collection / next-turn warmth. The redacting_tool fires distillation
    # as fire-and-forget so specialists return to the orchestrator without
    # the distiller round-trip on the critical path; here at end-of-turn
    # we wait for them so the KB / charts reflect the full set.
    pending = getattr(ctx, "_pending_distillers", None) or []
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

    # Phase 2 (revised) — surface charts in the reasoning-trace panel, NOT
    # inline in the chat. Each chart this turn is emitted as a typed `chart`
    # SSE event the frontend stores per-turn and renders alongside the
    # specialist's findings. Keeps the chat clean (text answer only) while
    # the trace gives reviewers click-to-open access to plots tied to the
    # specific finding that produced them.
    turn_charts = _collect_turn_charts(sess.specialist_kb, turn_id, sess.case_id)
    chart_payloads: list[dict] = []  # turn_id-less, reusable on cached replay
    if turn_charts:
        # Match each chart back to its KP for richer payload (claim,
        # source_call, vega_spec). The KB has the full record; we already
        # collected the chart URL/topic/specialist in `_collect_turn_charts`.
        for c in turn_charts:
            kp = _find_kp(sess.specialist_kb, c["specialist"], c["topic"], turn_id)
            chart_payloads.append({
                "specialist": c["specialist"],
                "topic": c["topic"],
                "url": c["url"],
                "claim": (kp or {}).get("claim", ""),
                "source_call": (kp or {}).get("source_call", ""),
                "kind": ((kp or {}).get("viz") or {}).get("kind", ""),
                "vega_spec": (kp or {}).get("vega_spec"),
            })
        for p in chart_payloads:
            sess.emit("chart", {**p, "turn_id": turn_id})
        sess.logger.log("turn_charts_emitted", {
            "turn_id": turn_id,
            "n_charts": len(chart_payloads),
            "topics": [p["topic"] for p in chart_payloads],
        })

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
            # Verbatim question text used by the relevance_check skill on
            # subsequent turns to spot near-duplicates of this one.
            "origin_question": verdict.redacted_question,
            # Chart payloads (turn_id-less) so cached-answer replays can
            # re-emit them under the new turn_id. PNG files persist under
            # reports/<case>/charts/ and serve fine on replay since the URL
            # is unchanged.
            "charts": chart_payloads,
        }
        sess.logger.log("qa_cache_store",
                        {"turn_id": turn_id, "norm_q": cache_key,
                         "answer_len": len(answer_text)})


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
            sess.turn_lock.acquire()  # block until available
        try:
            # `_run_turn_streamed` is structured to skip the early visible
            # emits when called from this path — see the `started_at`
            # parameter; the inner function uses that to drive duration
            # math without re-emitting reviewer_message / turn_started /
            # team_plan that we already fired above.
            asyncio.run(_run_turn_streamed(
                sess, turn_id, question, started_at=started_at,
            ))
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
    sess.input_history = []
    n_cached = len(sess.qa_cache)
    sess.qa_cache.clear()
    n_kb_specialists = len(sess.specialist_kb)
    n_kb_total = sum(len(v) for v in sess.specialist_kb.values())
    sess.specialist_kb.clear()
    sess.logger.log("rewind", {
        "message_id": msg_id, "case_id": case_id,
        "qa_cache_entries_cleared": n_cached,
        "kb_specialists_cleared": n_kb_specialists,
        "kb_kps_cleared": n_kb_total,
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
    return send_from_directory(charts_dir, filename, mimetype="image/png")


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
