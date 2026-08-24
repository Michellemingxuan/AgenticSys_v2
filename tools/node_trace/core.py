"""SQLite-backed per-node trace store.

Each row = one LLM-call boundary in the reasoning trace, keyed by
(chat_id, turn_id, node, started_at). See
docs/superpowers/specs/2026-05-21-node-trace-instrumentation-design.md.
"""
from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from tools.episodic import build_records

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_trace (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id              TEXT NOT NULL,
  case_id              TEXT NOT NULL,
  turn_id              TEXT NOT NULL,
  node                 TEXT NOT NULL,
  parent_id            INTEGER,
  depth                INTEGER NOT NULL,
  started_at           TEXT NOT NULL,
  ended_at             TEXT,
  duration_ms          INTEGER,
  queue_wait_ms        INTEGER,
  llm_call_ms          INTEGER,
  ttft_ms              INTEGER,
  overhead_ms          INTEGER,
  model                TEXT,
  prompt_chars         INTEGER,
  prompt_tokens        INTEGER,
  cached_input_tokens  INTEGER,
  system_prompt_chars  INTEGER,
  completion_chars     INTEGER,
  completion_tokens    INTEGER,
  reasoning_tokens     INTEGER,
  total_tokens         INTEGER,
  cost_usd             REAL,
  prompt_excerpt       TEXT,
  completion_excerpt   TEXT,
  messages_json        TEXT,
  output_json          TEXT,
  outcome              TEXT,
  error_type           TEXT,
  tags                 TEXT,
  extra_json           TEXT,
  conversation_id      TEXT,
  server_run_id        TEXT,
  user_id              TEXT,
  pillar_id            TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_turn ON node_trace(chat_id, turn_id, started_at);
CREATE INDEX IF NOT EXISTS idx_node      ON node_trace(node);
CREATE INDEX IF NOT EXISTS idx_started   ON node_trace(started_at);

-- End-of-turn snapshot of CaseSession's cross-turn state: qa_cache,
-- specialist_kb. Lets the viewer show what the
-- conversation "remembers" at each point. Cleared by delete_chat (rewind).
CREATE TABLE IF NOT EXISTS session_snapshot (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id       TEXT NOT NULL,
  case_id       TEXT NOT NULL,
  turn_id       TEXT NOT NULL,
  taken_at      TEXT NOT NULL,
  qa_cache_n          INTEGER,
  kb_specialists_n    INTEGER,
  kb_kps_n            INTEGER,
  -- RETIRED, always 0 / NULL. Raw chat history was once threaded into each
  -- orchestrator call; that was removed (every turn now starts fresh, and
  -- cross-turn context arrives via case summary + episodic block + KB warmth
  -- hint). The columns are kept so existing trace DBs need no migration.
  input_history_items INTEGER,
  input_history_chars INTEGER,
  qa_cache_json       TEXT,
  specialist_kb_json  TEXT,
  input_history_json  TEXT,
  qa_cache_raw_json   TEXT,
  conversation_id     TEXT,
  server_run_id       TEXT,
  user_id             TEXT,
  pillar_id           TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshot_chat_turn ON session_snapshot(chat_id, turn_id, taken_at);

CREATE VIEW IF NOT EXISTS turn_summary AS
  SELECT
    chat_id, case_id, turn_id,
    MIN(started_at) AS turn_started_at,
    MAX(ended_at)   AS turn_ended_at,
    SUM(prompt_tokens)       AS total_prompt_tokens,
    SUM(completion_tokens)   AS total_completion_tokens,
    SUM(cached_input_tokens) AS total_cached_tokens,
    SUM(reasoning_tokens)    AS total_reasoning_tokens,
    SUM(cost_usd)            AS total_cost_usd,
    COUNT(*)                 AS n_nodes,
    SUM(CASE WHEN depth = 1 THEN 1 ELSE 0 END) AS n_llm_rounds,
    SUM(CASE WHEN outcome IN ('failed','timeout') THEN 1 ELSE 0 END) AS n_failures
  FROM node_trace
  GROUP BY chat_id, case_id, turn_id;

CREATE VIEW IF NOT EXISTS session_summary AS
  SELECT
    chat_id, case_id,
    COUNT(DISTINCT turn_id) AS n_turns,
    SUM(prompt_tokens + completion_tokens) AS total_tokens,
    SUM(cost_usd) AS total_cost_usd,
    MIN(started_at) AS session_started_at,
    MAX(ended_at)   AS session_ended_at
  FROM node_trace
  GROUP BY chat_id, case_id;
"""


class NodeTraceStore:
    """Owns the SQLite connection + write lock for the node_trace table.

    Writes are serialized through ``_lock``; reads do not take the lock
    (WAL mode keeps readers non-blocking). Every public method swallows
    exceptions: telemetry must NEVER break an LLM call.
    """

    _ALLOWED_UPDATE_COLS = frozenset({
        "ended_at", "duration_ms", "queue_wait_ms", "llm_call_ms",
        "ttft_ms", "overhead_ms", "model",
        "prompt_chars", "prompt_tokens", "cached_input_tokens",
        "system_prompt_chars", "completion_chars", "completion_tokens",
        "reasoning_tokens", "total_tokens", "cost_usd",
        "prompt_excerpt", "completion_excerpt", "messages_json", "output_json",
        "outcome", "error_type", "tags", "extra_json",
    })

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._failure_logged = False
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False,
        )
        self._setup_journal_mode()
        try:
            self._conn.executescript(_SCHEMA)
        except RuntimeError:
            raise
        except sqlite3.OperationalError as exc:
            # If we got this far but schema apply failed, the DB file is
            # likely truly broken (corrupted / partial write from a prior
            # cloud-sync conflict). Surface a clear error so the user
            # knows what to do.
            raise RuntimeError(
                f"NodeTraceStore: schema apply failed on {db_path!r} ({exc}). "
                f"This usually means the DB file is corrupted (often from a "
                f"cloud-sync conflict). Delete the file and let it recreate, "
                f"or point NODE_TRACE_DB to a local path."
            ) from exc
        # Backfill column for DBs created before qa_cache_raw_json existed.
        try:
            self._conn.execute(
                "ALTER TABLE session_snapshot ADD COLUMN qa_cache_raw_json TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
        # Backfill identity columns for DBs created before conversation_id
        # existed (mirror the qa_cache_raw_json pattern). ADD COLUMN is
        # idempotent-by-try: OperationalError => the column is already there.
        for table in ("node_trace", "session_snapshot"):
            for col in ("conversation_id", "server_run_id", "user_id", "pillar_id"):
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass  # already exists

    def _setup_journal_mode(self) -> None:
        """Try WAL mode; fall back to DELETE on filesystems that reject it.

        WAL is faster + lets the viewer (reader) and server (writer)
        coexist without lock contention. But WAL uses memory-mapped files
        which DON'T work on every filesystem — notably Google Drive /
        iCloud / OneDrive cloud-synced directories return "disk I/O
        error" on ``PRAGMA journal_mode=WAL``. When that happens, fall
        back to DELETE mode (SQLite default) so the trace layer still
        works — at our write volume, serialized access is fine.
        """
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            self._log_failure(
                "wal_setup",
                Exception(
                    f"WAL mode rejected by filesystem ({exc}); falling back "
                    f"to default journal mode. If the DB is on a cloud-synced "
                    f"folder (Google Drive / iCloud), set NODE_TRACE_DB to a "
                    f"local path (e.g. ~/node_traces.db) for better performance."
                ),
            )
        try:
            self._conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.OperationalError:
            pass  # Sticking with whatever the connection picked.

    def insert(
        self,
        *,
        chat_id: str,
        case_id: str,
        turn_id: str,
        node: str,
        parent_id: int | None,
        depth: int,
        started_at: str,
        model: str | None = None,
        extra_json: str | None = None,
        conversation_id: str | None = None,
        server_run_id: str | None = None,
        user_id: str | None = None,
        pillar_id: str | None = None,
    ) -> int:
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO node_trace "
                    "(chat_id, case_id, turn_id, node, parent_id, depth, "
                    " started_at, model, extra_json, "
                    " conversation_id, server_run_id, user_id, pillar_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, case_id, turn_id, node, parent_id, depth,
                     started_at, model, extra_json,
                     conversation_id, server_run_id, user_id, pillar_id),
                )
                return int(cur.lastrowid or -1)
        except Exception as exc:  # noqa: BLE001
            self._log_failure("insert", exc)
            return -1

    def snapshot_session(
        self,
        *,
        chat_id: str,
        case_id: str,
        turn_id: str,
        qa_cache: Any,
        specialist_kb: Any,
        conversation_id: str | None = None,
        server_run_id: str | None = None,
        user_id: str | None = None,
        pillar_id: str | None = None,
    ) -> int:
        """Persist a snapshot of CaseSession's cross-turn state at end of turn.

        All payloads are stored as JSON. Counts are denormalized so the
        viewer's chat overview doesn't have to parse the JSON to show a
        summary. Like all telemetry, failures are swallowed.
        """
        try:
            qa_n = len(qa_cache) if qa_cache is not None else 0
            kb_specialists_n = (
                sum(1 for v in specialist_kb.values() if v)
                if isinstance(specialist_kb, dict) else 0
            )
            kb_kps_n = (
                sum(len(v or []) for v in specialist_kb.values())
                if isinstance(specialist_kb, dict) else 0
            )
            # Store the clean episodic projection the next turn actually
            # injects (question → sub-answers → final answer), NOT the raw
            # qa_cache entry. The raw entry also carries per-tool-call
            # payloads + chart specs (needed for cache-replay), but those are
            # noise in the "what the next turn remembers" viewer panel.
            # qa_cache_n above stays the true turn count (len of the cache).
            qa_json = json.dumps(
                build_records(qa_cache, window=qa_n),
                default=str,
            ) if qa_cache else None
            kb_json = json.dumps(specialist_kb, default=str) if specialist_kb else None
            qa_raw_json = json.dumps(qa_cache, default=str) if qa_cache else None
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO session_snapshot "
                    "(chat_id, case_id, turn_id, taken_at, "
                    " qa_cache_n, kb_specialists_n, kb_kps_n, "
                    " input_history_items, input_history_chars, "
                    " qa_cache_json, specialist_kb_json, input_history_json, "
                    " qa_cache_raw_json, "
                    " conversation_id, server_run_id, user_id, pillar_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, case_id, turn_id, _now_iso(),
                     qa_n, kb_specialists_n, kb_kps_n,
                     0, 0,
                     qa_json, kb_json, None,
                     qa_raw_json,
                     conversation_id, server_run_id, user_id, pillar_id),
                )
                return int(cur.lastrowid or -1)
        except Exception as exc:  # noqa: BLE001
            self._log_failure("snapshot_session", exc)
            return -1

    def case_activity(self) -> dict[str, dict]:
        """Per-case review activity: when it was last asked, and how many
        turns it holds.

        Backs the Overview list. Read from `session_snapshot` rather than from
        live sessions because the point is to describe cases NOBODY currently
        has open — a session is only built when a case is first touched, and
        building one per case just to read a date would restore every case's
        RAM as a side effect.

        Read-only, no lock, and never raises: an unavailable trace db means
        an Overview without dates, not a broken page.
        """
        try:
            rows = self._conn.execute(
                "SELECT case_id, MAX(taken_at) AS last_at, "
                "       MAX(qa_cache_n) AS turns "
                "FROM session_snapshot GROUP BY case_id",
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self._log_failure("case_activity", exc)
            return {}
        out: dict[str, dict] = {}
        for case_id, last_at, turns in rows:
            if case_id is None:
                continue
            out[str(case_id)] = {
                "last_qa_at": last_at,
                "turns": int(turns or 0),
            }
        return out

    def load_latest_snapshot(self, case_id: str) -> dict | None:
        """Return the most recent snapshot for a case as restorable state, or
        None. Read-only (no lock). Decodes each JSON column defensively."""
        try:
            row = self._conn.execute(
                "SELECT chat_id, qa_cache_raw_json, specialist_kb_json "
                "FROM session_snapshot "
                "WHERE case_id = ? ORDER BY taken_at DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            if row is None:
                return None
            chat_id, qa_raw, kb_json = row

            def _load(blob, default):
                if not blob:
                    return default
                try:
                    return json.loads(blob)
                except Exception:  # noqa: BLE001
                    return default

            return {
                "chat_id": chat_id,
                "qa_cache": _load(qa_raw, {}),
                "specialist_kb": _load(kb_json, {}),
            }
        except Exception as exc:  # noqa: BLE001
            self._log_failure("load_latest_snapshot", exc)
            return None

    def delete_chat(self, chat_id: str) -> int:
        """Drop every trace row for ``chat_id``. Returns rows removed.

        Telemetry must never break a real LLM call: errors are swallowed.
        Snapshots are dropped together (same lifetime as the trace rows).
        """
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM node_trace WHERE chat_id = ?", (chat_id,),
                )
                n_traces = int(cur.rowcount or 0)
                self._conn.execute(
                    "DELETE FROM session_snapshot WHERE chat_id = ?", (chat_id,),
                )
                return n_traces
        except Exception as exc:  # noqa: BLE001
            self._log_failure("delete_chat", exc)
            return 0

    def delete_turns(self, turn_ids: list[str]) -> int:
        """Drop trace rows for specific turn_ids. Returns rows removed."""
        if not turn_ids:
            return 0
        try:
            with self._lock:
                placeholders = ",".join("?" for _ in turn_ids)
                cur = self._conn.execute(
                    f"DELETE FROM node_trace WHERE turn_id IN ({placeholders})",
                    turn_ids,
                )
                n_traces = int(cur.rowcount or 0)
                self._conn.execute(
                    f"DELETE FROM session_snapshot WHERE turn_id IN ({placeholders})",
                    turn_ids,
                )
                return n_traces
        except Exception as exc:  # noqa: BLE001
            self._log_failure("delete_turns", exc)
            return 0

    def delete_case(self, case_id: str) -> int:
        """Drop every trace row for ``case_id`` — across all chat_ids /
        session ids that share this case. Returns rows removed.

        Each server process generates a NEW chat_id (session id) for the
        same case, so a chat-id-scoped delete leaves prior-process rows
        stranded. Rewind wipes by case_id so "clear means clear" matches
        the reviewer's mental model: this case's history is gone.
        """
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM node_trace WHERE case_id = ?", (case_id,),
                )
                n_traces = int(cur.rowcount or 0)
                self._conn.execute(
                    "DELETE FROM session_snapshot WHERE case_id = ?", (case_id,),
                )
                return n_traces
        except Exception as exc:  # noqa: BLE001
            self._log_failure("delete_case", exc)
            return 0

    def update(self, row_id: int, **fields: Any) -> None:
        if not fields or row_id <= 0:
            return
        clean = {k: v for k, v in fields.items() if k in self._ALLOWED_UPDATE_COLS}
        if not clean:
            return
        try:
            cols = ", ".join(f"{k} = ?" for k in clean)
            params = list(clean.values()) + [row_id]
            with self._lock:
                self._conn.execute(
                    f"UPDATE node_trace SET {cols} WHERE id = ?", params,
                )
        except Exception as exc:  # noqa: BLE001
            self._log_failure("update", exc)

    def _log_failure(self, op: str, exc: Exception) -> None:
        if self._failure_logged:
            return
        self._failure_logged = True
        import sys
        print(
            f"[node_trace] DB {op} failed once "
            f"({type(exc).__name__}: {exc}); subsequent failures suppressed.",
            file=sys.stderr,
        )


# ── Active-node + turn-scope contextvars ────────────────────────────────────

ACTIVE_NODE: contextvars.ContextVar["NodeTrace | None"] = contextvars.ContextVar(
    "ACTIVE_NODE", default=None,
)


@dataclass(frozen=True)
class TurnScope:
    chat_id: str
    case_id: str
    turn_id: str
    conversation_id: str = ""
    server_run_id: str = ""
    user_id: str = ""
    pillar_id: str = ""


TURN_SCOPE: contextvars.ContextVar["TurnScope | None"] = contextvars.ContextVar(
    "TURN_SCOPE", default=None,
)


def current_turn_scope() -> TurnScope | None:
    return TURN_SCOPE.get()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(text: str, head: int | None = None, tail: int | None = None) -> str:
    """Return head + ' … <N elided> … ' + tail. Configurable via env."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if os.environ.get("NODE_TRACE_FULL_PROMPT") == "1":
        return text
    h = head if head is not None else int(os.environ.get("NODE_TRACE_EXCERPT_HEAD", "400"))
    t = tail if tail is not None else int(os.environ.get("NODE_TRACE_EXCERPT_TAIL", "200"))
    if len(text) <= h + t:
        return text
    elided = len(text) - h - t
    return f"{text[:h]} … <{elided} chars elided> … {text[-t:]}"


# ── NodeTrace async context manager ─────────────────────────────────────────


def _hooks_own_rounds(node: "NodeTrace | None") -> bool:
    """True when ``NodeTraceRunHooks`` is responsible for creating round
    children under this NodeTrace (via the SDK's RunHooks lifecycle), so
    other code paths — notably ``firewall_client._FirewalledChatCompletions
    .create`` — must NOT also wrap each LLM call in a child NodeTrace.

    Without this flag, the streamed Runner's background task inherits
    ACTIVE_NODE via asyncio's context-copy and firewall_client double-
    wraps every model call, producing two rows per real call (one full
    via hook, one partial via NodeTrace.__aexit__ since streamed
    responses can't be model_dump_json'd the chat-completion way).
    """
    return bool(node is not None and getattr(node, "_hooks_own_rounds", False))


class NodeTrace:
    """Async context manager wrapping one node in the reasoning trace.

    On enter: INSERT a row with started_at + node + depth + parent_id
    (resolved from ACTIVE_NODE). Pushes self onto ACTIVE_NODE.

    On exit: UPDATE the row with ended_at + duration_ms + outcome +
    accumulated usage/latency/tags. Pops the contextvar.

    Accumulators (set via attach_usage / attach_tag / attach_latency /
    attach_io / attach_extra from LLM clients + agent wire-up sites).
    """

    def __init__(
        self,
        store: NodeTraceStore,
        *,
        chat_id: str,
        case_id: str,
        turn_id: str,
        node: str,
        depth: int,
        extra_json: str | None = None,
        conversation_id: str = "",
        server_run_id: str = "",
        user_id: str = "",
        pillar_id: str = "",
    ) -> None:
        self._store = store
        self.chat_id = chat_id
        self.case_id = case_id
        self.turn_id = turn_id
        self.node = node
        self.depth = depth
        self._extra_json = extra_json
        self.conversation_id = conversation_id
        self.server_run_id = server_run_id
        self.user_id = user_id
        self.pillar_id = pillar_id
        self.row_id: int = -1
        self._token: contextvars.Token | None = None
        self._t0: float = 0.0
        self._round_count: int = 0
        # Token-count accumulators
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0
        self.cached_input_tokens: int = 0
        self.reasoning_tokens: int = 0
        # Sizing
        self.prompt_chars: int = 0
        self.system_prompt_chars: int = 0
        self.completion_chars: int = 0
        # Excerpts + full I/O
        self.prompt_excerpt: str = ""
        self.completion_excerpt: str = ""
        self.messages_json: str | None = None
        self.output_json: str | None = None
        # Latency split
        self.queue_wait_ms: int | None = None
        self.llm_call_ms: int | None = None
        self.ttft_ms: int | None = None
        # Meta
        self.model: str | None = None
        self.cost_usd: float = 0.0
        self.tags: list[str] = []
        # Set externally (e.g. by server.py when passing
        # NodeTraceRunHooks to Runner.run_streamed) to signal that this
        # node's children come from the SDK hook path and should NOT
        # also be wrapped by firewall_client's contextvar path.
        self._hooks_own_rounds: bool = False

    def next_round_index(self) -> int:
        self._round_count += 1
        return self._round_count

    async def __aenter__(self) -> "NodeTrace":
        parent = ACTIVE_NODE.get()
        if parent is not None:
            self.conversation_id = self.conversation_id or getattr(parent, "conversation_id", "")
            self.server_run_id = self.server_run_id or getattr(parent, "server_run_id", "")
            self.user_id = self.user_id or getattr(parent, "user_id", "")
            self.pillar_id = self.pillar_id or getattr(parent, "pillar_id", "")
        # conversation_id is the grouping axis; fall back to chat_id so a row
        # is never left ungrouped (legacy/test call sites that pass only chat_id).
        self.conversation_id = self.conversation_id or self.chat_id
        self._t0 = perf_counter()
        self.row_id = self._store.insert(
            chat_id=self.chat_id,
            case_id=self.case_id,
            turn_id=self.turn_id,
            node=self.node,
            parent_id=parent.row_id if parent and parent.row_id > 0 else None,
            depth=self.depth,
            started_at=_now_iso(),
            extra_json=self._extra_json,
            conversation_id=self.conversation_id,
            server_run_id=self.server_run_id,
            user_id=self.user_id,
            pillar_id=self.pillar_id,
        )
        self._token = ACTIVE_NODE.set(self)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            duration_ms = int((perf_counter() - self._t0) * 1000)
            outcome = "ok" if exc_type is None else "failed"
            overhead_ms: int | None = None
            if self.queue_wait_ms is not None or self.llm_call_ms is not None:
                accounted = (self.queue_wait_ms or 0) + (self.llm_call_ms or 0)
                overhead_ms = max(0, duration_ms - accounted)
            tags_payload = json.dumps(self.tags) if self.tags else None
            self._store.update(
                self.row_id,
                ended_at=_now_iso(),
                duration_ms=duration_ms,
                queue_wait_ms=self.queue_wait_ms,
                llm_call_ms=self.llm_call_ms,
                ttft_ms=self.ttft_ms,
                overhead_ms=overhead_ms,
                outcome=outcome,
                error_type=exc_type.__name__ if exc_type else None,
                model=self.model,
                prompt_tokens=self.prompt_tokens or None,
                completion_tokens=self.completion_tokens or None,
                cached_input_tokens=self.cached_input_tokens or None,
                reasoning_tokens=self.reasoning_tokens or None,
                total_tokens=self.total_tokens or None,
                cost_usd=self.cost_usd or None,
                prompt_chars=self.prompt_chars or None,
                system_prompt_chars=self.system_prompt_chars or None,
                completion_chars=self.completion_chars or None,
                prompt_excerpt=self.prompt_excerpt or None,
                completion_excerpt=self.completion_excerpt or None,
                messages_json=self.messages_json,
                output_json=self.output_json,
                tags=tags_payload,
                extra_json=self._extra_json,
            )
        finally:
            if self._token is not None:
                ACTIVE_NODE.reset(self._token)
                self._token = None
        return None  # don't suppress exceptions


# ── attach_* helpers (write onto the active node) ───────────────────────────


def attach_usage(
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    prompt_excerpt: str | None = None,
    completion_excerpt: str | None = None,
    prompt_chars: int | None = None,
    system_prompt_chars: int | None = None,
    completion_chars: int | None = None,
    model: str | None = None,
    cost_usd: float | None = None,
) -> None:
    """Write usage/excerpt fields onto the ACTIVE_NODE if one is set."""
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    if prompt_tokens is not None:
        nt.prompt_tokens = prompt_tokens
    if completion_tokens is not None:
        nt.completion_tokens = completion_tokens
    if total_tokens is not None:
        nt.total_tokens = total_tokens
    elif prompt_tokens is not None or completion_tokens is not None:
        nt.total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    if cached_input_tokens is not None:
        nt.cached_input_tokens = cached_input_tokens
    if reasoning_tokens is not None:
        nt.reasoning_tokens = reasoning_tokens
    if prompt_excerpt is not None:
        nt.prompt_excerpt = _excerpt(prompt_excerpt)
        nt.prompt_chars = len(prompt_excerpt)
    if prompt_chars is not None:
        nt.prompt_chars = prompt_chars
    if system_prompt_chars is not None:
        nt.system_prompt_chars = system_prompt_chars
    if completion_excerpt is not None:
        nt.completion_excerpt = _excerpt(completion_excerpt)
        nt.completion_chars = len(completion_excerpt)
    if completion_chars is not None:
        nt.completion_chars = completion_chars
    if model is not None:
        nt.model = model
    if cost_usd is not None:
        nt.cost_usd = cost_usd


def attach_tag(*tags: str) -> None:
    """Append tag(s) to the active node. No-op when no active node."""
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    for t in tags:
        if t and t not in nt.tags:
            nt.tags.append(t)


def attach_latency(
    *,
    queue_wait_ms: int | None = None,
    llm_call_ms: int | None = None,
    ttft_ms: int | None = None,
) -> None:
    """Record per-segment latency on the active node. queue_wait_ms is
    additive (one node may pass through the semaphore multiple times on
    retry); llm_call_ms overwrites; ttft_ms is first-write-wins."""
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    if queue_wait_ms is not None:
        nt.queue_wait_ms = (nt.queue_wait_ms or 0) + queue_wait_ms
    if llm_call_ms is not None:
        nt.llm_call_ms = llm_call_ms
    if ttft_ms is not None and nt.ttft_ms is None:
        nt.ttft_ms = ttft_ms


def attach_io(
    *,
    messages_json: str | None = None,
    output_json: str | None = None,
) -> None:
    """Store the full structured input / output on the active node.

    Always on (the whole point of the trace DB is to let you read the
    actual prompts for optimization work). Two escape hatches:
      NODE_TRACE_NO_IO=1            — skip entirely
      NODE_TRACE_MAX_IO_BYTES=<n>   — skip individual payloads above n bytes
                                      (default 1_048_576 = 1 MB).
    Pathological mega-payloads (cached schema dumps, etc.) are stored as a
    tiny stub so the row still records "we had something here, but it was
    over the cap".
    """
    if os.environ.get("NODE_TRACE_NO_IO") == "1":
        return
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    max_bytes = int(os.environ.get("NODE_TRACE_MAX_IO_BYTES", str(1_048_576)))
    if messages_json is not None:
        nt.messages_json = (
            messages_json if len(messages_json) <= max_bytes
            else f'{{"_elided": "payload >{max_bytes}B; raise NODE_TRACE_MAX_IO_BYTES to capture", "size": {len(messages_json)}}}'
        )
    if output_json is not None:
        nt.output_json = (
            output_json if len(output_json) <= max_bytes
            else f'{{"_elided": "payload >{max_bytes}B; raise NODE_TRACE_MAX_IO_BYTES to capture", "size": {len(output_json)}}}'
        )


def attach_extra(**kv: Any) -> None:
    """Merge keys into the active node's extra_json."""
    nt = ACTIVE_NODE.get()
    if nt is None:
        return
    try:
        existing = json.loads(nt._extra_json) if nt._extra_json else {}
    except Exception:
        existing = {}
    existing.update(kv)
    nt._extra_json = json.dumps(existing)


# ── _open_node / _NullNode convenience for wire-up sites ────────────────────


class _NullNode:
    """No-op async context manager returned by ``_open_node`` when either
    the store or the turn scope is missing — keeps call sites uncluttered.
    """
    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): return None


def _open_node(store: NodeTraceStore | None, node: str, depth: int = 0):
    """Build a ``NodeTrace`` from the ambient ``TURN_SCOPE`` + the store.

    Returns ``_NullNode()`` when either is missing — wire-up sites stay
    clean and tests that don't set up a store still pass.
    """
    scope = TURN_SCOPE.get()
    if store is None or scope is None:
        return _NullNode()
    return NodeTrace(
        store=store,
        chat_id=scope.chat_id,
        case_id=scope.case_id,
        turn_id=scope.turn_id,
        node=node,
        depth=depth,
        conversation_id=scope.conversation_id,
        server_run_id=scope.server_run_id,
        user_id=scope.user_id,
        pillar_id=scope.pillar_id,
    )
