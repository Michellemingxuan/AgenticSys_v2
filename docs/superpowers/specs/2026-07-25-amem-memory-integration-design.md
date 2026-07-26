# Amem Memory Integration — Design Spec

Date: 2026-07-25
Status: Draft (for review)
Owner: server / orchestration / memory
Phase: 1 of 2 (Amem-first; full multi-session is Phase 2)

## 1. Problem

Cross-turn memory today is **RAM-only, single-session, single-process**, and both
of its channels are lossy:

- **`specialist_kb` (KnowledgePoints)** — injected into the orchestrator every turn
  by `_format_kb_warmth_hint` (`runner/turn/input_assembly.py:26-52`), which dumps
  **every specialist × every active topic**, each claim **clipped to 120 chars**
  and **collapsed to one KP per topic** (`active[topic] = kp`). `kb_list_topics`
  clips to 100 chars (`tools/kb_tools.py:91`). The full claim is retrievable only
  via `kb_lookup(topic)` on an **exact slug match** (`tools/kb_tools.py:117`) — no
  semantic/fuzzy retrieval. Net effect: the orchestrator context is simultaneously
  **bloated** (everything is always loaded) and **truncated** (nothing is loaded in
  full), and an agent that can't guess the exact slug gets nothing.

- **`qa_cache` → episodic** — only the **newest 3 turns** (`EPISODIC_TURNS=3`,
  `tools/episodic.py:11`) are injected, from a window of 10, answers truncated to
  800 chars / sub-answers to 400. Anything older than 3 turns is invisible unless
  the question is re-asked **verbatim** (the exact-match `_replay_from_cache`). A
  follow-up 8 turns later that is semantically related to turn 1 sees nothing.

There is no durable, cross-session, relevance-ranked memory. `Amem` (sibling repo
`../Amem`) is a five-layer memory infrastructure built for exactly this shape
(working / conversation / case / user / organization, hybrid embedding+keyword
search, Qdrant-in-Docker to avoid I/O locking). This spec integrates Amem as an
**API-like backing store** that raises retrieval quality without disrupting the
existing hot path.

## 2. Goals / Non-goals

**Goals**
- Replace "load everything, truncated" with "retrieve the relevant few, in full"
  for both KPs and prior Q&A, via Amem hybrid search.
- Make case memory **durable and cross-session** (survives new sessions), keyed so
  rewind stays correct.
- Keep the existing RAM dicts (`specialist_kb`, `qa_cache`) as the **O(1) hot path**;
  Amem is **additive** and **failure-isolated** — Amem/Qdrant down ⇒ turn behaves
  exactly as today.
- Add a **session-start memory brief** (or first-session welcome) surfaced to the
  reviewer.
- Wire Amem for **both** environments (prod safechain / dev OpenAI) behind one
  backend-aware factory.

**Non-goals (this phase)**
- Full multi-session lifecycle: multiple concurrent sessions per case, session
  list/switch UI, resuming a specific past session. (**Phase 2.**)
- Modifying Amem's core `MemoryScope` (session is carried in metadata, not a new
  scope field).
- `user`/`organization` Amem layers and cross-case learning (`user_id` is a stub
  constant this phase).
- Changing the exact/near-duplicate `_replay_from_cache` behavior (it stays a RAM
  fast path).

## 3. Architecture overview

Amem is consumed as an **in-process `AmemManager` facade** (Python import of the
`Amem` package from the parent dir), talking to **Qdrant Server over HTTP
(`:6333`)** — the Docker service is what provides concurrent writes/reads without
file locking. AgenticSys never touches Qdrant directly.

```text
TurnRunner / agent_tool ── AmemManager (in-process) ── HTTP ──> Qdrant :6333 (Docker)
        │                                                              │
   RAM dicts (hot path: specialist_kb, qa_cache)              durable + vector search
```

Coupling model (decided): **hybrid — dict for hot, Amem working-layer for search.**
The RAM dicts remain the exact/hot reads the existing tools depend on; Amem
`working`-layer records mirror them so semantic search covers even un-consolidated
in-turn memory, and `conversation`/`case` layers provide durable cross-session
retrieval.

Every Amem call is wrapped in try/except. **Writes** are fire-and-forget
(`asyncio.create_task`, drained best-effort at turn end, same pattern as the
existing distiller/auto-chart tasks). **Reads** on the critical path get a tight
timeout (~1.5s) and fall back to today's exact behavior.

## 4. Session shim & scope mapping

Mint a durable `session_id` on each `CaseSession` (`server.py:152`) at creation
(one active session per case, as today). Carry it into the `TurnScope` contextvar
(`runner/turn/conductor.py:175`) alongside `chat_id`/`case_id`/`turn_id`.

Amem `MemoryScope` mapping (Amem core untouched):

| Amem field | Source | Notes |
|---|---|---|
| `org_id` | constant `"amx"` | single-tenant this phase |
| `user_id` | constant `"amx_reviewer"` | stub; real reviewer identity is future work |
| `case_id` | `case_id` | direct |
| `turn_id` | `turn_id` (uuid hex[:12]) | direct; rewind delete key |
| `agent_id` | specialist name / `"orchestrator"` | KPs by specialist; final Q/A by orchestrator |
| `session_id` | minted per `CaseSession` | **stored in `metadata`**, not a scope field |

- **Reads (retrieval)** filter by `case_id` only ⇒ span all sessions (cross-session
  durability).
- **Deletes (rewind)** filter by `case_id + turn_id` (both real scope fields) via
  `list_memories` + per-id `delete_memory`. **Correction after reading the Amem
  store code:** Amem's `list`/`search`/`delete` filter ONLY by scope fields
  (org/user/case/turn/agent) + level + kind — there is NO metadata filtering, and
  `delete_memory` is single-id. So the current session's turns are deleted by their
  `turn_id`s (which the server already tracks); `session_id` in metadata is
  provenance only, not a delete key.

## 5. Rewind / clear semantics under durable Amem

| Operation | Endpoint | RAM | Amem |
|---|---|---|---|
| **cancel-turn** (abort in-flight) | `POST .../cancel-turn` (`server.py:893`) | clear current turn (as today) | delete current turn's records (`case+session+turn`) |
| **partial rewind** (`removeTurnIds`) | `POST .../rewind` (`server.py:961`, `is_partial`) | drop those turns (as today) | delete those turns (`case+session+turn`) |
| **full rewind / clear-history** | `POST .../rewind` (no `removeTurnIds`) | wipe RAM (as today) | **preserve**; advance `session_id` (new session) — prior session becomes immutable |

Consequence (confirmed by design intent): after clear-history, re-asking an old
question no longer exact-replays from RAM (`qa_cache` wiped), but Amem **may surface
the prior answer as retrieved context** — the intended cross-session benefit. All
Amem deletes are best-effort; a delete failure logs and never blocks the endpoint.

## 6. Write seams

All writes fire-and-forget, drained at turn end, never block, never break a turn.

1. **KP → Amem `working`** — at the distiller seam `_distill_and_persist`
   (`agent_factories/agent_tools/agent_tool.py:453`). Mirror each `KnowledgePoint`
   append (the dict write is unchanged) via `arecord_tool_result` / working-layer
   add, `agent_id = specialist`. Provides in-turn hot semantic search coverage.

2. **Turn Q/A + KPs → Amem `conversation`** — at `_finalize`
   (`runner/turn/conductor.py:1151`, next to the existing `_store_cached_qa` at
   `:1342`). `arecord_conversation(raw_question, final_answer, atomic_facts=<turn's KPs>,
   scope=case+turn+agent="orchestrator", metadata={session_id})`. Passing KPs as
   `atomic_facts` skips a redundant LLM extraction. This is the **durable, cross-session,
   per-turn KP store** ("stores by turn and also serves as atomic facts").

3. **`case` consolidation → Amem `case`** — `aupsert_case_memory(scope=case)`
   fire-and-forget after the conversation write, **every turn**. Feeds the
   session-start brief (§8) and the case-layer read (§7).

## 7. Read seams (the two quality wins)

Both replace the lossy injectors and both fall back to today's behavior on
timeout/error.

1. **KP look-up** — in `assemble_orchestrator_input`
   (`runner/turn/input_assembly.py:60`), replace the bulk `_format_kb_warmth_hint`
   with `asearch_related(question, levels=[working, conversation], scope=case,
   search_mode="hybrid", limit≈6)`. Inject the **relevant few in full** — no
   120-char clip, no one-per-topic collapse. On Amem miss/timeout, fall back to the
   current warmth hint.

2. **`kb_lookup` semantic fallback** — in `tools/kb_tools.py:101`, when the exact
   slug match fails, run an Amem hybrid search scoped to the case and return the
   best match (still labeled by topic/specialist/confidence) instead of
   `"not found"`. Exact-slug path unchanged when it hits.

3. **Case-memory follow-up** — replace the `EPISODIC_TURNS=3` newest-3 injection
   (`tools/episodic.py` consumers in `input_assembly.py:76-84`) with
   `asearch_related` over `conversation`/`case` memory ranked by the current
   question (no recent-3 cap), full answers. On Amem miss/timeout, fall back to the
   current recency-based episodic block. (The pure `tools/episodic.py` builder stays
   as the fallback path; the selector becomes Amem-backed when available.)

## 8. Session-start memory brief

On **new-session open** (first `CaseSession` build for a case, or after a
clear-history advances `session_id`), retrieve case-level memory from Amem
(`list_memories(levels=[CASE], scope=case)` for the `case_summary`, plus recent
`conversation` highlights / counts, scope = `case_id`, cross-session):

- **Memory exists** → emit a `session_brief` SSE event summarizing what's known
  about the case so far (reference anchor for the reviewer).
- **First session / no memory** → `"Welcome to the discovery journey of case <case_id>."`
- **Amem down** → fall back to the welcome line.

Emitted once at session creation and appended to `event_buffer` (`server.py:194`)
so a client that connects after session open still receives it (consistent with the
"alternate paths must replay full SSE" rule for buffered frames).

## 9. Dual environment wiring

Mirrors the existing `llm/factory.py::build_session_clients` backend split
(openai / safechain). Introduce one **backend-aware Amem manager factory** built
alongside session clients (CLI: `main.py:121`; server: `server.py:269`).

- **prod (safechain)** — wire the two stub factories in
  `Amem/integrations/safechain.py` (`create_embedding_model`, `create_language_model`)
  to AgenticSys's safechain client (reuse the `amodel` path from
  `llm/safechain_client.py`). Subject to the **firewall/safechain parity rule** —
  any change mirrored across `firewall_client.py` and `safechain_client.py`.
- **dev (openai)** — **DONE (Amem side):** `Amem/integrations/openai_runtime.py`
  provides `OpenAIEmbeddingProvider`, `OpenAILanguageModel`, `OpenAIConfig`, and
  `create_openai_manager(...)` — the parallel of `create_safechain_manager`,
  exported from `Amem.integrations`. Uses real `text-embedding-3-large` (3072),
  **not** the deterministic `HashingEmbeddingProvider`. Unlike safechain (expiring
  clients → fresh-model-per-call), it reuses one long-lived `AsyncOpenAI` client and
  relies on SDK retries, with a per-call `asyncio.wait_for` for budget.
  - **`embedding_client` split:** `FirewalledAsyncOpenAI` (`llm/firewall_client.py:178`)
    exposes only `.chat`, not `.embeddings`. So AgenticSys passes the firewalled
    client for LLM synthesis (`client=`) and a plain `AsyncOpenAI` for embeddings
    (`embedding_client=`). Safe because memory content is already redacted upstream
    (agent_tool payload redaction / question screening) before it becomes a memory.
  - Remaining AgenticSys-side work: the backend-aware factory that calls
    `create_openai_manager` (dev) vs `create_safechain_manager` (prod) and injects
    the right clients + `AMEM_STORE_URL`.
- **Vector size** `3072` both envs (`AMEM_VECTOR_SIZE`) — must match the embedding
  model dimension or Amem fails fast on dimension validation.

## 10. Infrastructure & failure isolation

- **Qdrant-in-Docker** on `:6333` (both envs). AgenticSys connects via
  `AMEM_STORE_URL`. At bootstrap, a health check probes the store; unreachable ⇒
  Amem features **cleanly disabled** (a null-object manager whose reads return
  empty and writes no-op), and every seam falls back to today's behavior.
- Every Amem call try/except-wrapped and logged via `EventLogger`. Honors the
  **screen <5s / single-specialist ≤20s** wall-clock budgets. The added retrieval
  cost is **one embed call + one Qdrant search** on the orchestrator-input path
  (the main latency risk), covered by the ~1.5s timeout + fallback.

## 11. Environment prerequisites

- **`qdrant-client`** must be installed in the `autoAI` dev env
  (`~/.pyenv/versions/3.11.13/envs/autoAI/bin/python`) — currently missing. Add to
  `requirements.txt`.
- **Docker** (or Amem's standalone Qdrant binary) required to run a real Qdrant
  service — Docker is not on PATH in the current dev shell. **Dev unit tests do NOT
  require Docker** (they use an in-memory/fake `MemoryStore`); only integration/real
  runs need the service.

## 12. Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `AMEM_ENABLED` | `1` | Master switch; `0` ⇒ null-object manager (today's behavior) |
| `AMEM_STORE_URL` | `http://127.0.0.1:6333` | Qdrant service endpoint |
| `AMEM_COLLECTION_NAME` | `amem_memories` | Qdrant collection |
| `AMEM_VECTOR_SIZE` | `3072` | Embedding dimension (must match model) |
| `AMEM_READ_TIMEOUT_S` | `1.5` | Critical-path retrieval timeout before fallback |
| `AMEM_RETRIEVE_LIMIT` | `6` | top-K for KP + case-memory retrieval |
| `AMEM_ORG_ID` / `AMEM_USER_ID` | `amx` / `amx_reviewer` | Scope stubs |

Amem's own vars (`AMEM_STORE_BACKEND`, `AMEM_QDRANT_*`, `AMEM_PROMPT_DIR`) continue
to apply per the Amem README.

## 13. Testing strategy (dev-only; safechain mocked)

Interpreter: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python`. Store: fake /
in-memory `MemoryStore` (no Docker). Embeddings: `HashingEmbeddingProvider` (or
OpenAI where quality matters). SafeChain wiring mocked (safechain uninstallable in
dev) — raise concrete prod questions before finalizing safechain specifics.

Assertions:
1. **Dual-write** fires on both seams (working at distiller, conversation+case at
   `_finalize`) with correct scope + `session_id` metadata.
2. **Retrieval returns full untruncated** claims (no 120/100-char clip, not
   one-per-topic).
3. **Amem-down falls back byte-for-byte** to today's warmth hint + recency episodic
   (null-object manager path).
4. **Partial rewind / cancel-turn** propagate deletes to Amem (`case+session+turn`);
   **clear-history** advances `session_id` and **preserves** prior records.
5. **Session brief**: emits `session_brief` when case memory exists; emits the
   welcome line on first session / Amem-down; frame is buffered for late subscribers.
6. **`kb_lookup`** exact-slug path unchanged; semantic fallback returns a labeled
   match on slug miss.

## 14. Phasing / out of scope

- **Phase 1 (this spec):** everything above.
- **Phase 2 (later spec):** full multi-session — `SESSIONS` keyed by `session_id`,
  multiple concurrent sessions per case, session list/switch/resume API + UI,
  richer session-level briefs. The `user`/`organization` Amem layers and cross-case
  learning are also deferred.

## 15. Risks & open questions

- **Retrieval latency** on the orchestrator-input path (one embed + one search per
  turn) is the primary budget risk; mitigated by timeout+fallback and small top-K.
  Validate against the <5s/≤20s budgets in dev before prod.
- **SafeChain embedding dimension** must equal `AMEM_VECTOR_SIZE=3072` — confirm the
  prod safechain embedding model's dimension (prod question).
- **SafeChain concurrency**: Amem creates a fresh model per call; the existing
  4×-slower-under-concurrency Azure-throttling caveat may apply to added embed
  calls — monitor.
- **Consolidation frequency** (every turn) may create write load; revisit to
  every-N-turns if needed.
- **Cross-session replay expectations**: confirm reviewers want a re-asked question
  to surface prior-session context (intended) vs. a truly blank slate on
  clear-history.

## 16. File-touch map

| File | Change |
|---|---|
| `Amem/integrations/openai_runtime.py` | **DONE** — OpenAI providers + `create_openai_manager` (dev path) |
| `Amem/integrations/__init__.py` | **DONE** — export the OpenAI symbols |
| `Amem/integrations/safechain.py` | Wire embedding + language model factories (prod) |
| `llm/factory.py` (or new `llm/amem_factory.py`) | Backend-aware `AmemManager` factory (dev→`create_openai_manager`, prod→`create_safechain_manager`) + null-object fallback |
| `server.py` | `session_id` on `CaseSession`; session-brief emit; Amem deletes in rewind/cancel/clear paths; bootstrap health check |
| `runner/turn/conductor.py` | `TurnScope.session_id`; conversation+case writes in `_finalize` |
| `agent_factories/agent_tools/agent_tool.py` | Working-layer KP mirror at `_distill_and_persist` |
| `runner/turn/input_assembly.py` | Amem-backed KP look-up + case-memory read (with fallback) |
| `tools/kb_tools.py` | `kb_lookup` semantic fallback |
| `tools/episodic.py` | Stays as fallback selector; Amem-backed selector wraps it |
| `requirements.txt` | Add `qdrant-client` |
| `tests/` | New `test_amem_*` suites per §13 |
```
