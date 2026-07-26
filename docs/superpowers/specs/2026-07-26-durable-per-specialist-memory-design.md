# Durable Per-Specialist Memory + Load-Once/Write-Once — Design Spec

Date: 2026-07-26
Status: Draft (for review)
Owner: memory / conductor
Relationship: Phase 3 of the Amem work. Reshapes the KP persistence + load path to the
user's **DB=source-of-truth, RAM=working-set, load-once/write-once** model.

## 1. Problem / goal

Today: KPs live in the RAM `specialist_kb` dict (accumulated across turns), are mirrored
mid-turn to Amem's `working` layer (RAM-only → invisible in the viewer, not durable), and
the only durable Amem record is the orchestrator turn Q/A (with KPs flattened to
`atomic_facts`). So the viewer shows only `agent_id=orchestrator`, and there is no durable
per-specialist memory nor a reload-from-Amem path.

Goal (the user's model):
```
Question arrives → ONE batched read from Amem → RAM working set
   (current turn + last N episodes + relevant active KPs + exact-answer LRU)
Orchestrator + specialists reuse RAM — no per-step DB queries mid-turn
Turn completes → ONE batched write back to Amem
```
DB (Amem) is the durable source of truth; RAM is the fast working set.

## 2. Design

### 2.1 Durable per-specialist conversation record (write-once, at turn end)
For each specialist that ran this turn, the batched `_finalize` write emits ONE Amem
`conversation` record:

```
scope:  org=amx, user=amx_reviewer, case_id, turn_id, agent_id=<specialist>
raw_question:  the specialist's sub-question
raw_answer:    the specialist's findings
atomic_facts:  that specialist's KP claim strings (durable, human-readable, embedded)
metadata:
  session_id
  knowledge_points: [ <full KP dict: topic, claim, numbers, confidence, viz,
                       source_call, captured_at_turn> ]   ← structured, for faithful reload
  tool_calls: [ {func: "summarize_trend", params: {...}}, ... ]  ← func+params only, NO payloads
```
The orchestrator turn record (final Q/A) and the `case` summary are still written.

### 2.2 Load specialist_kb from Amem per turn (load-once)
At turn start, ONE batched query reconstructs the RAM dict:
`load_case_kps(amem, cfg, case_id)`:
- `list_memories(scope=MemoryScope(org,user,case_id), levels=["conversation"])`
  (metadata/`case_id`-scoped — no semantic dependency; Amem semantic search not relied on yet).
- Group each record's `metadata.knowledge_points` by `record.scope.agent_id`, ordered by
  `created_at` (chronological, so the existing "latest KP per topic = active" logic holds).
- Returns `{agent_id: [kp_dict, ...]}` → assigned to `sess.specialist_kb`.
- Never raises → `{}` on error (falls back to empty / snapshot).

**Forward-compat:** the ONLY change to move to advanced retrieval later is swapping the
`list_memories` in `load_case_kps` for `asearch_related(query, scope=case_id,
search_mode="hybrid", ...)`. The records are embedded, so semantic/hybrid KP relevance works
then; the structured KPs in metadata make reconstruction identical regardless of selection.

### 2.3 RAM during the turn (unchanged working set)
The `specialist_kb` dict (loaded per 2.2), `qa_cache` (episodic + exact-answer LRU) stay the
in-RAM working set. The distiller still appends KPs to the dict during the turn (RAM). No
mid-turn Amem writes.

### 2.4 Removed: mid-turn Amem working mirror
Delete the `mirror_kp_working` call at the distiller seam (and its now-unused helper).
KPs persist ONLY via the batched end-of-turn write (2.1). Amem's `working` level is no longer
used by this integration.

### 2.5 Collecting per-specialist data during the turn (for the batched write)
At the agent-tool seam (`agent_tool._runner`, where the specialist `result` is available),
collect per specialist into a new `AppContext._specialist_turn_records[name]`:
`{sub_question, findings, tool_calls}` where `tool_calls` = the func names + JSON-parsed
arguments extracted from the specialist run's function-call items (data tools only; NOT
report_agent, NOT the payloads). `_finalize` reads this + the turn's KPs
(`kps_for_turn` per agent) and writes one record per specialist.

## 3. Raw tool payloads (explicitly NOT in Amem)
Raw tool-call payloads stay in RAM during the turn + the `node_trace` SQLite DB (audit) and
are released from RAM at turn end. Amem holds the curated signal (Q/A, KPs, tool func+params),
not scratch. (See prior decision.)

## 4. File-touch map
| File | Change |
|---|---|
| `memory/loader.py` (new) | `load_case_kps(amem, cfg, *, case_id) -> dict` |
| `memory/writer.py` | `write_specialist_memory(...)`; keep `write_conversation`/`consolidate_case`; drop `mirror_kp_working` use |
| `memory/__init__.py` | export `load_case_kps`, `write_specialist_memory`; drop `mirror_kp_working` export if unused |
| `models/app_context.py` | add `_specialist_turn_records: dict = {}` |
| `agent_factories/agent_tools/agent_tool.py` | extract `{func,params}` from `result`; stash `{sub_question, findings, tool_calls}` on ctx; remove distiller working-mirror |
| `agent_factories/agent_tools/distiller_pass.py` | remove `_mirror_kp` / `mirror_kp_working` call (KP→RAM dict stays) |
| `runner/turn/conductor.py` | `_finalize`: batched per-specialist writes (+ orchestrator + case) |
| `server.py` | `_get_or_create_session`/turn start: load `specialist_kb` from Amem via `load_case_kps` (replaces snapshot-KB restore when Amem is on) |
| `tests/memory/…` | loader + writer + reconstruct-roundtrip + no-mirror guard |

## 5. Testing
- `write_specialist_memory`: record has agent_id=specialist, atomic_facts=claims,
  metadata.knowledge_points=full dicts, metadata.tool_calls=[{func,params}].
- `load_case_kps`: round-trip — write N per-specialist records (via FakeAmem), load →
  reconstruct `{agent_id: [kps]}`, latest-per-topic preserved; empty/error → `{}`.
- Distiller no longer calls `mirror_kp_working` (source guard / behavior).
- `_finalize` batched write emits one record per specialist that ran + orchestrator + case.
- Live e2e: ask a multi-specialist question → viewer shows per-specialist memory with KPs +
  tool_calls; restart → specialist_kb reloads from Amem; follow-up reuses it.

## 6. Open items / future
- Semantic/hybrid KP load (§2.2 swap) once Amem search is validated.
- "Last N episodes" load into RAM from Amem (currently episodic is from qa_cache/snapshot);
  could move to an Amem-backed batched load under the same load-once step — deferred.
- Exact-answer LRU stays in qa_cache/snapshot for now.
