# Restart-Invisible node_trace via a Deterministic `conversation_id`

**Date:** 2026-07-26
**Status:** Design approved — ready for implementation plan
**Author:** brainstormed with the reviewer

## Goal

Make the node_trace audit view **restart-invisible**: one reviewer's ongoing
investigation of a case appears as a single, continuous conversation whose
turns stitch together across any number of server restarts. Along the way,
put the identity model on a footing that is forward-compatible with
**multi-user** and **multi-pillar** without building either yet.

## Background — the problem

node_trace keys every row by `chat_id`, and `chat_id` is fed from
`sess.logger.session_id` (`runner/turn/conductor.py:185`). That session id is
minted **fresh on every server process**:

```python
# server.py:571
case_logger = EventLogger(session_id=f"case-{case_id}-{uuid.uuid4().hex[:6]}")
```

So the *same* investigation of the *same* case gets a **new `chat_id` after
every restart**, and the viewer — which groups and routes by `chat_id`
(`tools/node_trace/viewer.py`: `/`, `/chat/<chat_id>`, `/state/<chat_id>`,
`/turn/<chat_id>/<turn_id>`) — shows it as two unrelated chats. The trace is
"very helpful for audit and monitor," but a restart fractures it.

**Root cause:** `chat_id` conflates two distinct roles — the *durable
conversation identity* and the *ephemeral server-run identity*. The fix is to
separate them.

**Amem is not affected and needs no structural change.** Amem never made this
mistake: durable memory is scoped by `case_id` (stable) + `org_id`/`user_id`
(constants today), and the per-process session id lives only in *metadata*,
never in the scope filter (`Amem/core/models.py:41` — `MemoryScope(org_id,
user_id, case_id, turn_id, agent_id)`). So Amem is already restart-invisible:
last run's memory persists in Qdrant, and a new run appends new `turn_id`s
under the same `(org, user, case)`. This spec leaves Amem's behavior entirely
intact — no writes to the memory layer at all.

## The identity model

Three separated concepts, replacing the overloaded `chat_id`:

| Concept | Field | Lifetime | Role |
|---|---|---|---|
| **investigation** | `conversation_id` | durable, deterministic | the group/route axis — what a "conversation" is |
| **server process** | `server_run_id` | one per process | diagnostic only (which deployment produced a turn) |
| **question** | `turn_id` | one per user question | unchanged |

Supporting attribution columns carried alongside (for viewer filtering and
future multi-user), all stable today:

| Field | Source today | Today's value |
|---|---|---|
| `case_id` | request | the case being reviewed |
| `user_id` | `AMEM_USER_ID` | `amx_reviewer` (constant) |
| `pillar_id` | `PILLAR` env (`runner/config.py:11`) | `credit_risk` (constant, server-wide) |

### `conversation_id` is **derived, never minted**

```
conversation_id = f"{case_id}::{user_id}::{pillar_id}"
```

It is **computed** whenever those three inputs are known (at session open and
on every turn) — it is not created at an explicit event, not persisted, and
not looked up. Consequences:

- **Restart-invisible for free.** A restart recomputes the *same*
  `conversation_id` from the *same* inputs → the viewer shows one group. No
  "recover latest by case" lookup, no persistence.
- **Multi-user falls out.** `user_id` in the key → two reviewers on the same
  case get distinct conversations automatically.
- **Multi-pillar falls out.** Switching `credit_risk → escalation` yields a
  different `conversation_id` automatically — matching "reopen, maybe pillar
  changed = new session."
- **Today it collapses to 1:1 with `case_id`**, because `user_id` and
  `pillar_id` are server constants. That is exactly today's correct behavior,
  with the future seats reserved.

### `server_run_id`

One value per server process, minted at import. `server.py:269` already
creates `_BOOT_LOGGER = EventLogger(session_id=f"server-{uuid...}")`; this
spec introduces a dedicated module constant (e.g. `_SERVER_RUN_ID =
f"run-{uuid.uuid4().hex[:8]}"`) for clear intent. It is written on every row
as a **diagnostic column** and shown per turn in the viewer — but it is
**never** a grouping key. Its asymmetry with the memory layer is intentional:
memory is restart-invisible by construction, so `server_run_id` has no Amem
analog.

### Reopen semantics (decided)

Reopening the **same** `(case, user, pillar)` later **continues the same
conversation** — same `conversation_id`, memory intact, traces stitch as one
thread. This is the pure-composite behavior and the approved default. The one
thing the composite cannot express — "same pillar, but a deliberately *fresh*
session" — is explicitly **out of scope** (see Deferred), and would later add a
persisted `epoch` component: `f(case, user, pillar, epoch)`.

## Architecture & components

The change is small because it is a root-cause fix: correct the value feeding
the grouping axis, then carry the diagnostic/attribution fields additively.

### New module: `runner/identity.py`

Single-responsibility, pure, unit-testable. Used by `server.py` (session
open) and available to `conductor.py` (via the session).

```python
SEP = "::"

def compose_conversation_id(case_id: str, user_id: str, pillar_id: str) -> str:
    """Deterministic conversation identity. Same inputs → same id, forever,
    across restarts. Readable for the audit viewer."""
    return f"{case_id}{SEP}{user_id}{SEP}{pillar_id}"

SERVER_RUN_ID: str  # module-level, minted once per process at import
```

`user_id` source: `AmemConfig.user_id` when available, else
`os.environ.get("AMEM_USER_ID", "amx_reviewer")` (safe default so a disabled
Amem does not blank the id). `pillar_id` source: `PILLAR`.

### `CaseSession` (`server.py`)

Add one field, computed at construction (`_get_or_create_session`, near
`server.py:593`):

```python
conversation_id: str = ""      # deterministic; == compose_conversation_id(...)
```

`sess.session_id` (the per-process case logger id) is retained for logging and
Amem metadata, but is **no longer** the node_trace grouping axis.

### `TurnScope` (`tools/node_trace/core.py`)

Extend the ambient turn-scope so `_open_node` → `NodeTrace` → `insert` can
write the new fields without threading them through call sites. To avoid
churn at the ~5 existing `chat_id` read-sites, keep a `chat_id` that mirrors
`conversation_id`:

```python
@dataclass(frozen=True)
class TurnScope:
    conversation_id: str
    server_run_id: str
    case_id: str
    turn_id: str
    user_id: str = ""
    pillar_id: str = ""

    @property
    def chat_id(self) -> str:        # back-compat shim
        return self.conversation_id
```

Construction site (`conductor.py:184`) changes from
`chat_id=sess.logger.session_id` to reading `sess.conversation_id`,
`SERVER_RUN_ID`, `sess.case_id`, and the `user_id`/`pillar_id` off config.

### node_trace schema (`tools/node_trace/core.py`)

Additive columns on **both** `node_trace` and `session_snapshot`, applied via
the existing `ALTER TABLE ... ADD COLUMN` migration pattern (the same
try/except used for `qa_cache_raw_json`):

- `conversation_id TEXT`
- `server_run_id TEXT`
- `user_id TEXT`
- `pillar_id TEXT`

`chat_id` **stays** and, on every new row, is written equal to
`conversation_id`. This means:

1. Legacy rows (old per-process `chat_id`) remain viewable, grouped as they
   were originally recorded.
2. Every existing query/tool that filters by `chat_id`
   (`turn_report.py`, `optimization_report.py`, the `turn_summary` /
   `session_summary` views) keeps working **unchanged** and immediately
   benefits from restart-invisibility, because `chat_id` now holds a
   deterministic value.
3. New code uses the explicit `conversation_id` column.

`insert()` and `snapshot_session()` gain the four new parameters and write
them. `NodeTrace.__aenter__` passes them through from `TurnScope`.

### Deletes (rewind / clear-history)

`delete_case(case_id)` and `delete_turns(turn_ids)` remain correct **today**
because `case_id ≡ conversation_id` under single-user/single-pillar. On full
rewind, `sess.conversation_id` is **not** rotated (it is derived and stable);
the memory + traces are hard-deleted instead, which Phase-2 already does
(`delete_case_memory` + `delete_case`). The obsolete
`sess.session_id = f"case-{case_id}-{uuid...}"` rotation on full rewind
(`server.py:1118`) is no longer load-bearing for conversation identity; it may
remain for Amem-metadata immutability or be removed — the implementation plan
decides, but it must not reintroduce a per-process grouping key.

*Forward path (documented, not built):* when multi-user lands, deletes scope
by `conversation_id` instead of `case_id`, so clearing one user's
investigation does not touch another user's conversation on the same case.

### Viewer (`tools/node_trace/viewer.py`)

- **Grouping axis** becomes `conversation_id`, with
  `COALESCE(conversation_id, chat_id)` so legacy rows still appear.
- **Relabel** the UI from "chat" to "conversation": index list, route names
  (`/conversation/<id>` with `/chat/<id>` kept as an alias/redirect for any
  bookmarked links), and headers.
- **Surface `server_run_id`**: on the turn list and the turn detail, show
  which server run produced each turn, so a conversation that spans restarts
  is visibly one thread annotated with run boundaries — "server restart should
  be invisible to users" as continuity, while still *diagnosable* by run.
- Optional (nice-to-have, not required): show `user_id` / `pillar_id` on the
  conversation list for future multi-tenant filtering.

### Amem alignment

- **Now:** Amem is left **entirely untouched** — no metadata stamping, no
  scope-field change, no retrieval-filter change. It is already restart-safe
  (retrieval is `case_id`-scoped, and the per-process session id lives only in
  metadata, never in the scope filter), so nothing is required for the
  restart-invisibility goal.
- **Deferred (documented):** share `conversation_id` across layers — stamp it
  into Amem metadata and/or promote it from metadata to a first-class
  `MemoryScope` field, then add `user_id` + `conversation_id` to the retrieval
  filters in `load_case_kps` / `load_latest_snapshot`. Amem's scope already
  reserves `org_id`/`user_id`, so this is a small future change.

This change is **backend-agnostic** (node_trace + TurnScope + identity are not
part of the OpenAI/safechain LLM path), so there is no
`firewall_client`/`safechain_client` parity obligation here.

## Data flow (new)

```
server import
  └─ SERVER_RUN_ID minted once  (runner/identity.py)

open case (_get_or_create_session)
  └─ sess.conversation_id = compose_conversation_id(case_id, user_id, pillar_id)   # deterministic

each turn (TurnRunner.__init__)
  └─ TURN_SCOPE.set(TurnScope(
         conversation_id = sess.conversation_id,     # stable across restarts
         server_run_id   = SERVER_RUN_ID,            # this process
         case_id, turn_id, user_id, pillar_id))

each node (NodeTrace via _open_node)
  └─ insert(conversation_id, server_run_id, user_id, pillar_id,
            chat_id = conversation_id,               # shim
            case_id, turn_id, ...)

restart → reopen same case
  └─ conversation_id recomputes to the SAME value → viewer shows ONE conversation,
     turns annotated by differing server_run_id.
```

## Scope

**In scope**
1. `runner/identity.py`: `compose_conversation_id`, `SERVER_RUN_ID`.
2. `CaseSession.conversation_id` computed at open.
3. `TurnScope` extended (+ `chat_id` shim); construction site updated.
4. node_trace schema: 4 additive columns on `node_trace` + `session_snapshot`,
   migrated; `insert`/`snapshot_session`/`NodeTrace` write them;
   `chat_id = conversation_id` on new rows.
5. Viewer: group/route by `conversation_id` (COALESCE legacy), relabel to
   "conversation," surface `server_run_id` per turn.
6. Tests: deterministic-id, restart-invisibility (two runs → one group),
   migration on a pre-existing DB, viewer grouping, rewind-across-restart.

**Out of scope / deferred (documented in-code where relevant)**
- Real multi-user auth and per-request `user_id` wiring.
- Amem `conversation_id` as a scope field + retrieval-filter changes.
- Per-session pillar switching (pillar is server-wide today).
- Reopen-**fresh** semantics and the persisted `epoch` component.

## Key decisions & rationale

- **Derived, not minted `conversation_id`.** Determinism is what buys
  restart-invisibility with zero persistence/lookup. It also makes multi-user
  and multi-pillar emerge from the key rather than from new plumbing.
- **`chat_id = conversation_id` shim instead of a rename.** Fixes the bug at
  the source (the value), keeps ~5 read-sites and all CLI reports working
  unchanged, and avoids a risky column rename — while new code uses the
  explicit name.
- **`server_run_id` is diagnostic-only.** Grouping by it would re-create the
  bug; it exists to *diagnose* deployment/runtime issues, and its absence from
  Amem mirrors that memory is durable by design.
- **Continue-on-reopen.** Approved default; the `epoch` escape hatch is
  deferred under YAGNI.
- **Amem untouched.** It is already restart-safe, so this work changes nothing
  in the memory layer; cross-layer `conversation_id` sharing is deferred.

## Testing strategy

- `runner/identity.py`: `compose_conversation_id` is pure → exact-string
  assertions; `SERVER_RUN_ID` is a non-empty stable-within-process string.
- **Restart-invisibility (the headline test):** insert trace rows under
  `SERVER_RUN_ID = run-A`, then again under `run-B`, for the same
  `(case, user, pillar)`; assert both land under **one** `conversation_id`
  group and that `turn_summary`/the viewer's conversation query returns them
  together, with two distinct `server_run_id`s.
- **Migration:** open a `NodeTraceStore` on a DB created *before* the new
  columns; assert the `ADD COLUMN`s apply idempotently and legacy rows still
  read (grouped by `COALESCE(conversation_id, chat_id)`).
- **Rewind a pre-restart turn (regression guard).** Insert trace rows +
  a session snapshot for two turns under `server_run_id = run-A`; simulate a
  restart by starting a fresh store/session under `run-B` and restoring state
  from the case-keyed snapshot (`load_latest_snapshot`); then partial-rewind
  **one** of the pre-restart turns (`delete_turns([turn_A1])` on node_trace +
  Amem, and drop its `qa_cache`/`specialist_kb` entries by
  `turn_id_origin`/`captured_at_turn`). Assert: the rewound turn's trace rows
  and Amem memory are **gone**, the *other* pre-restart turn's rows + memory
  **survive**, and the surviving turn still groups under the same
  `conversation_id`. This proves rewind reaches across the restart boundary
  (deletes key on stable `turn_id`/`case_id`, never on `server_run_id`).
- Full suite must stay green (currently 651 passing); no new failures.
```
