# Durable Case Session + Per-Case Clear History — Design Spec

Date: 2026-07-26
Status: Draft (for review)
Owner: server / session / frontend
Relationship: Phase 2 of the Amem work. Builds on the Phase-1 session shim
(`session_id`, session-brief, Amem scoping). **Not** a multi-session/​switcher
feature — the user's model is "opening a case = opening its one ongoing session,"
made durable across server restarts.

## 1. Problem

Today a case has exactly one session (`SESSIONS[case_id]`), but it is **not
durable**:

- The backend's cross-turn memory (`qa_cache`, `specialist_kb`, `_qa_turn_seq`,
  `input_history`) lives only in the `CaseSession` RAM object. A **server restart
  wipes it** — reopening a case rebuilds an empty session, so the agent has lost
  the conversation's context even though Amem (Qdrant) still holds the durable
  learned memory.
- The **conversation thread** (visible Q&A) lives only in the browser's
  `localStorage` (Zustand `persist`, `CaseReviewChat/src/store.ts`). It survives a
  page refresh, but it is browser-local and **not server-authoritative**: after a
  server restart the browser still shows the thread while the server has forgotten
  it — display and backend memory **diverge**. On another device the thread is
  absent entirely.

Separately, the **"Clear History" button is global**: `Sidebar.tsx`'s
`handleClearHistory` full-rewinds *every* case on the server and clears *all* cases
from the browser. The user wants it scoped to the active case.

## 2. Goals / Non-goals

**Goals**
- **Restart-invisible sessions.** Reopening a case after a server restart restores
  the backend's conversation memory (`qa_cache` + `specialist_kb`) so the
  investigation continues seamlessly; the user cannot tell the server restarted.
- **Server-authoritative thread.** The backend serves the conversation thread from
  durable storage, so the displayed conversation matches restored memory and works
  cross-device (not only from one browser's `localStorage`).
- **Per-case Clear History.** "Clear History" clears only the case being viewed.
- **Snapshot consistency on rewind.** The persisted snapshot always reflects the
  case's current post-rewind state, so a cleared/rewound case stays that way after
  a restart.

**Non-goals**
- Multiple concurrent sessions per case, a session list/switcher, or resuming a
  *specific past* session. (One durable session per case only.)
- Changing Amem *read* scoping (reads stay case-scoped/cross-session; `session_id`
  in metadata unchanged).

## 3. Architecture overview

The durable state already exists: `NodeTraceStore.session_snapshot` (SQLite)
persists `qa_cache_json`, `specialist_kb_json`, `input_history_json` per turn,
keyed by `(chat_id, case_id, turn_id)` with a `taken_at` timestamp
(`tools/node_trace/core.py:63-77`, written by `snapshot_session()` at
`conductor._finalize`). Amem holds the learned knowledge in Qdrant. Neither is
lost on restart — only the in-RAM `CaseSession` is.

```text
Case open  ──▶ _get_or_create_session ──▶ restore RAM from latest snapshot ──▶ continue
Thread view ──▶ GET /history ──▶ reconstruct from restored qa_cache (server-authoritative)
Clear (per-case) ──▶ full rewind THIS case: wipe RAM + snapshot, PRESERVE Amem
```

The only missing pieces are: a **read path** for snapshots (there is
`snapshot_session`/`delete_*` but no loader), a **restore step** on session
creation, a **history endpoint**, **snapshot cleanup on rewind**, and the
**frontend** scoping + history fetch.

## 4. Backend — restore session memory on case open

**Snapshot fidelity fix (prerequisite).** `snapshot_session` currently stores the
*projected* qa_cache (`build_records(qa_cache)` — a list of episodic records) into
`qa_cache_json`, **not** the raw `qa_cache` dict the runtime needs (keyed by
normalized question; carries replay payload + `turn_seq`). `specialist_kb_json` and
`input_history_json` *are* stored raw. So to restore faithfully we must **also
persist the raw qa_cache**:
- Add a nullable column `qa_cache_raw_json TEXT` to `session_snapshot` (in `_SCHEMA`
  for fresh DBs **and** an idempotent `ALTER TABLE … ADD COLUMN` migration guarded
  by try/except for existing DBs — the user's DB already exists).
- `snapshot_session` writes `json.dumps(qa_cache, default=str)` into it (cheap; it
  already has the dict). Keep `qa_cache_json` as the projection so the trace viewer
  is unaffected.

Add `NodeTraceStore.load_latest_snapshot(case_id) -> dict | None` (the first read
method on the store — mirror the write methods' `try/except self._log_failure`
shape but WITHOUT `self._lock`; `self._conn` has no `row_factory`, so SELECT
explicit columns): most recent row for `case_id` (`ORDER BY taken_at DESC LIMIT 1`),
parsed into `{"qa_cache": {...}, "specialist_kb": {...}, "input_history": [...],
"chat_id": ...}` from `qa_cache_raw_json` / `specialist_kb_json` /
`input_history_json`; `None` when the case has no snapshot. JSON-decode each column
defensively (decode error ⇒ empty, log, never raise).

In `server.py::_get_or_create_session`, after building the fresh `CaseSession`
and before `SESSIONS[case_id] = sess`, restore:

```python
snap = _NODE_TRACE_STORE.load_latest_snapshot(case_id) if _NODE_TRACE_STORE else None
if snap:
    sess.qa_cache = snap["qa_cache"] or {}
    sess.specialist_kb = snap["specialist_kb"] or {}
    sess.input_history = snap["input_history"] or []
    sess._qa_turn_seq = max((e.get("turn_seq", 0) for e in sess.qa_cache.values()), default=0)
    sess.logger.log("session_restored", {"case_id": case_id,
        "qa_entries": len(sess.qa_cache),
        "kb_specialists": len(sess.specialist_kb)})
```

- `_qa_turn_seq` continues from the restored max so new turns keep ascending
  ordering (episodic depends on it).
- `session_id`: keep the freshly-minted one (new process → new `chat_id`). Amem
  reads are case-scoped, so memory continuity is unaffected and the change is
  invisible. Traces simply start a new `chat_id` after restart (acceptable).
- The existing session-brief emit (Phase 1) still fires on creation — now
  alongside the restored thread.

## 5. Backend — keep the snapshot consistent on rewind

`snapshot_session` is append-per-turn and restore reads the **latest**. So after a
rewind mutates RAM, the latest snapshot must reflect the new state or a restart
would resurrect removed turns.

- **Full rewind / Clear History** (`post_rewind` full branch only): `delete_case`
  is **already wired** here (server.py:1107 — deletes `node_trace` +
  `session_snapshot`). Add the Amem purge `delete_case_memory(...)` next to it (§7).
  Nothing to restore ⇒ a cleared case reopens truly empty. **`post_cancel_turn` is
  per-turn, NOT a case clear** — it aborts the in-flight turn and already deletes
  only that turn's Amem via `delete_turns([current_turn_id])` (server.py:959-964);
  leave it as-is (no `delete_case`/`delete_case_memory`).
- **Partial rewind** (`post_rewind` with `removeTurnIds`): after removing those
  turns from `qa_cache`/`specialist_kb`, write a fresh snapshot of the reduced
  state so restore reflects it:
  `_NODE_TRACE_STORE.snapshot_session(chat_id=sess.logger.session_id, case_id=…,
  turn_id=f"rewind-{max_turn_seq}", qa_cache=sess.qa_cache,
  specialist_kb=sess.specialist_kb, input_history=sess.input_history)`. The removed
  turns' Amem records are deleted by the existing Phase-1 `delete_turns(...)` call
  (kept), and their trace rows via `delete_turns(removeTurnIds)` as today.

Guard every store call so a persistence failure logs and never breaks the
rewind/turn.

## 6. Backend — history endpoint (server-authoritative thread)

Add `GET /api/cases/<case_id>/history` → reconstruct the thread from the (restored)
`qa_cache`, ordered by `turn_seq` ascending. For each entry emit two messages:

```json
{"messages": [
  {"role": "reviewer", "text": "<origin_question>", "turn_id": "<turn_id_origin>"},
  {"role": "agent",    "text": "<answer>",          "turn_id": "<turn_id_origin>"}
]}
```

Pure read from `sess.qa_cache` (call `_get_or_create_session` first so restore has
run). No LLM, no Amem. This is what makes "conversation maintained" hold across
devices and cleared browser caches, not just same-browser `localStorage`.

## 7. Clear History & Rewind memory semantics

**Both operations remove the corresponding memory** — the intuitive undo/reset model.

**Clear History = full reset of the case** (forget everything):

| Store | On Clear History (a case) |
|---|---|
| RAM (`qa_cache`, `specialist_kb`, `input_history`) | cleared |
| `session_snapshot` for the case | deleted (`delete_case`) — stays cleared after restart |
| **Amem** (`working`/`conversation`/`case` for the case) | **purged** (all levels) |
| `session_id` | rotated (clean slate) |

The Amem purge needs a whole-case delete (not just by turn_id). Add
`delete_case_memory(amem, cfg, case_id) -> int` to `memory/rewind.py`:
`list_memories(scope=build_scope(cfg, case_id), include_working=True)` across all
levels → `delete_memory(rec.id)` for each; never raises. Wire it into the
**full-rewind branch of `post_rewind` only** (next to the existing `delete_case`
at server.py:1107). `post_cancel_turn` stays per-turn (unchanged).

**Rewind to a point = undo those turns:**

| Store | On Rewind (removeTurnIds) |
|---|---|
| RAM: those turns' `qa_cache` entries + KP `captured_at_turn` matches | removed (Phase 1) |
| `session_snapshot` | re-snapshotted to the reduced state (§5) |
| **Amem** for the removed `turn_id`s | deleted via existing `delete_turns(...)` (Phase 1) |

So Rewind already removes memory for the turns it undoes — this spec keeps that and
adds the snapshot-consistency step. After either operation, the session-brief and
retrieval reflect only what remains.

## 8. Frontend — per-case Clear History

`CaseReviewChat/src/components/Sidebar/Sidebar.tsx::handleClearHistory`: operate on
`activeCase` only.

```js
async function handleClearHistory() {
  if (!activeCase) return
  await postRewind(activeCase, '').catch((err) =>
    console.error(`Failed to clear server cache for case ${activeCase}`, err))
  clearCaseHistory(activeCase)   // new store action, replaces global clearHistory()
}
```

Add a store action `clearCaseHistory(caseId)` (`src/store.ts`) that resets only
`threads[caseId]`, `turns[caseId]`, `activeTurnId[caseId]`, and this case's
`unread` — leaving other cases untouched. Keep the old global `clearHistory` only
if something else uses it (grep shows only the Sidebar; otherwise remove it).
Update the button label/placement if it currently reads as global (e.g. "Clear
this case's history").

## 9. Frontend — load thread on case open

In `src/store.ts::setActiveCase` (or the Workspace effect that reacts to
`activeCase`), when a case is selected, fetch `GET /api/cases/<id>/history` and
populate `threads[id]` from it (server-authoritative), then let the live SSE
(`useSSE`) append new turns on top. Reconcile with `localStorage`: treat the
server response as the source of truth for already-completed turns; keep the
`persist` cache only as a fast first paint. Add `fetchHistory(caseId)` to
`src/api.ts`.

Idempotency: `startTurn`/`appendMessage` are already turn-id-idempotent
(`store.ts` guards), so a history load followed by SSE replay must not duplicate
messages — key rendered messages by `turn_id`/message id.

## 10. Amem interaction

Unchanged. Reads stay case-scoped (cross-session); `session_id` metadata still
written; the session brief still emits on case open. Restoring `qa_cache` also
restores **episodic** continuity (episodic is derived from `qa_cache`), so
follow-up coreference works again after a restart.

## 11. Edge cases & risks

- **Snapshot/thread drift**: if the browser `localStorage` thread and the server
  snapshot disagree (e.g. a turn persisted server-side but the browser closed
  first), §9 makes the **server authoritative**, resolving it.
- **Large qa_cache**: capped at `_QA_CACHE_MAX_ENTRIES=1024`; restoring + history
  reconstruction is O(entries) — fine. If a case ever exceeds that, history shows
  the retained window (log if truncated).
- **Restore performance**: one indexed SQLite read on case open (`idx_snapshot_chat_turn`);
  negligible. Honor the existing session-creation path budget.
- **Concurrent restore**: `_get_or_create_session` holds `SESSIONS_LOCK`; restore
  happens inside it, so no double-restore race.
- **Corrupt JSON in a snapshot column**: decode defensively → empty + log; never
  block session creation.

## 12. Testing (dev; `autoAI` interpreter)

- `load_latest_snapshot`: returns latest by `taken_at`; `None` when absent;
  defensive on corrupt JSON.
- `_get_or_create_session` restore: seed a snapshot, open the case, assert
  `qa_cache`/`specialist_kb`/`_qa_turn_seq` restored; no snapshot ⇒ empty session
  (today's behavior).
- Rewind consistency: full rewind ⇒ `delete_case` called ⇒ reopening yields empty;
  partial rewind ⇒ fresh snapshot reflects reduced state.
- History endpoint: reconstructs ordered reviewer/agent messages from `qa_cache`;
  empty case ⇒ `{"messages": []}`.
- Clear History purges Amem: `delete_case_memory` deletes all levels for the case
  (working/conversation/case); after full rewind, a fake Amem shows the case's
  records deleted. Rewind purges only the removed turns' Amem records.
- Frontend (vitest): `clearCaseHistory` resets only the target case; `fetchHistory`
  populates the thread; SSE replay after history load doesn't duplicate by turn_id.

## 13. File-touch map

| File | Change |
|---|---|
| `tools/node_trace/core.py` | add `load_latest_snapshot(case_id)` |
| `memory/rewind.py` | add `delete_case_memory(amem, cfg, case_id)` (whole-case Amem purge) |
| `server.py` | restore in `_get_or_create_session`; on full rewind + cancel: `delete_case` + `delete_case_memory`; re-snapshot on partial rewind; `GET /api/cases/<id>/history` |
| `CaseReviewChat/src/api.ts` | add `fetchHistory(caseId)` |
| `CaseReviewChat/src/store.ts` | add `clearCaseHistory(caseId)`; load history in `setActiveCase` |
| `CaseReviewChat/src/components/Sidebar/Sidebar.tsx` | per-case `handleClearHistory` + label |
| `CaseReviewChat/src/types.ts` | store action types |
| `tests/…`, `CaseReviewChat/src/__tests__/…` | per §12 |

## 14. Resolved decisions

1. **Clear History / Rewind memory** — Clear History purges the case's Amem memory;
   Rewind purges the undone turns' Amem memory (§7).
2. **History endpoint + frontend fetch** (§6, §9) — **included** (server-authoritative,
   cross-device). This is in scope.
3. **`session_id` continuity** — a fresh internal `chat_id` per restart (simple;
   traces split across restarts, invisible to the user). Revisit only if
   continuous per-session traces are needed.

## 15. Future direction — discrete, closable sessions (NOT this spec)

Recorded so this design stays forward-compatible. A later feature: the user
**"closes the file" (finishes the investigation)** to end a session, and can
**open a new session** on the same case later (e.g. time window changed, or a
different `pillar`), investigating it afresh as a distinct thread.

Forward-compatibility notes (things this spec must not break):
- Snapshots are **already per-session** (`session_snapshot` keyed by `chat_id` =
  session id). The durable-case-session here is effectively "the case's *latest*
  session." Extending to many discrete sessions = add a session registry +
  list/create/**close**/reopen + a `session_id` dimension on the API/frontend +
  optionally a **per-session `pillar`** (today `pillar` is server/CLI-level). So
  this is an *extension*, not a rewrite — keep `session_id` a clean, standalone
  concept (don't fuse it into `case_id`).
- **Amem has no native `session_id`** (scope = org/user/case/turn/agent);
  `session_id` lives in `metadata` and is **not filterable** by
  `list`/`search`/`delete`. The current model is case-scoped, so this is fine now.
  When discrete sessions land and want **session-scoped** memory (retrieve/delete a
  single session's memory, or isolate sessions), promote `session_id` to a
  first-class Amem `MemoryScope` field (modify the Amem repo — `core/models.py`
  scope + `db/qdrant.py` filter builder) or map it onto an existing field. Until
  then, keep writing `session_id` into metadata (already done) so the data is there
  when we need it.
