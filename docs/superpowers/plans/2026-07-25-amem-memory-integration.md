# Amem Memory Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Back AgenticSys_v2's cross-turn memory (`specialist_kb` KnowledgePoints + `qa_cache`/episodic) with the Amem five-layer store so retrieval becomes full-claim, relevance-ranked, and durable across sessions — additively, with the RAM dicts kept as the hot path and a clean fallback when Amem is unavailable.

**Architecture:** A new AgenticSys `memory/` package wraps Amem's `AmemManager` (in-process facade → Qdrant-in-Docker over HTTP). All Amem I/O goes through small, defensive helpers (config, factory+null-object, scope, writer, reader, rewind, brief). Writes are mirrored at the distiller seam (working layer) and at turn finalize (conversation + case layers); reads replace the bulk KB-warmth dump and the newest-3 episodic injection with `asearch_related`. Every seam degrades to today's exact behavior when Amem is disabled or unreachable.

**Tech Stack:** Python 3.11 (`autoAI` pyenv virtualenv), `openai-agents` SDK, Amem (`../Amem`, imported as `Amem`), `qdrant-client`, Qdrant Server (Docker) on `:6333`, OpenAI (`text-embedding-3-large`, 3072-dim) in dev / SafeChain in prod, pytest.

## Global Constraints

- Test interpreter is the pyenv virtualenv `autoAI`: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python`. Bare `python` (base 3.11.13) lacks matplotlib → collection errors. Run pytest as `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest ...`.
- **PREREQUISITE (done before Task 3):** Amem is made importable as the top-level package `Amem` via an **editable install** into the `autoAI` env: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pip install -e "../Amem"`. This matches the repo's existing sibling-project pattern (`FAIM`/`VeriChain` `.pth`), works for BOTH `pytest` and runtime (`python server.py`), and auto-installs `qdrant-client>=1.18.0` (Amem's own dependency — so plan Task 9 is largely satisfied by this install; keep the `requirements.txt` line for explicitness). Verified: bare `import Amem` works and the full suite collects 598 tests. (An earlier draft's claim that Amem was "already importable" was wrong — it required a manual `sys.path` insert.)
- Amem manager methods are **keyword-only** (note the `*` in every signature). Scope filtering is exact-match on set fields only; `MemoryScope.as_dict(include_none=False)` drops `None` fields (a `None` field = "don't filter on it"). There is **no** metadata filtering in `list`/`search`; `delete_memory(id)` is single-id only.
- `MemoryLevel` string values: `"working"`, `"conversation"`, `"case"`, `"user"`, `"organization"`. Pass strings or `MemoryLevel` members interchangeably (methods `coerce`).
- Scope constants this phase: `org_id="amx"`, `user_id="amx_reviewer"` (env-overridable). `session_id` is stored in Amem **metadata**, never as a scope field.
- NEVER commit or push unless the human asks in the current turn. Each task ends with a commit **step** the human runs — do not execute it autonomously beyond what the executing skill's checkpoints allow.
- Every Amem call is wrapped so a failure logs and falls back; an Amem error must never break a turn, a rewind, or session creation.
- `text-embedding-3-large` → `AMEM_VECTOR_SIZE=3072`. Store vector size is derived from `embedding_provider.dimensions`; a mismatch fails fast.

---

## File Structure

**New package `memory/` (AgenticSys side):**
- `memory/__init__.py` — re-exports the public helpers.
- `memory/config.py` — `AmemConfig` (env-driven settings).
- `memory/null_manager.py` — `NullAmemManager` (no-op manager implementing the used surface).
- `memory/scope.py` — `build_scope(...)`, `base_metadata(...)`, `kps_for_turn(...)`.
- `memory/factory.py` — `build_amem_manager(cfg, backend)` + store health check.
- `memory/writer.py` — `mirror_kp_working`, `write_conversation`, `consolidate_case` (async, defensive).
- `memory/reader.py` — `retrieve_context`, `search_kp` (async, defensive, timeout+fallback).
- `memory/rewind.py` — `delete_turns(amem, cfg, case_id, turn_ids)` (sync).
- `memory/brief.py` — `build_session_brief(amem, cfg, case_id)` (sync).
- `tests/memory/_fake_amem.py` — shared `FakeAmem` test double.
- `tests/memory/test_*.py` — unit tests per module.

**Modified (seam edits):**
- `requirements.txt` — add `qdrant-client`.
- `server.py` — build the manager at bootstrap; `session_id` + `current_turn_id` on `CaseSession`; session-brief emit; Amem deletes / session rotation in rewind/cancel; close on shutdown.
- `models/app_context.py` — add `_amem`, `_amem_cfg`, `_case_id`, `_session_id` fields.
- `runner/turn/conductor.py` — populate the new `AppContext` fields; async retrieval in `_assemble_input`; conversation+case writes in `_finalize`.
- `runner/turn/input_assembly.py` — accept `amem_block`, prefer it over the bulk warmth hint.
- `agent_factories/agent_tools/distiller_pass.py` — mirror each appended KP into Amem working layer.
- `tools/kb_tools.py` — semantic fallback in `kb_lookup` on exact-slug miss.

---

## Task 1: `AmemConfig` — env-driven settings

**Files:**
- Create: `memory/__init__.py`
- Create: `memory/config.py`
- Test: `tests/memory/test_config.py`

**Interfaces:**
- Produces: `AmemConfig` frozen dataclass with fields `enabled: bool`, `store_url: str`, `collection_name: str`, `vector_size: int`, `read_timeout_s: float`, `write_timeout_s: float`, `retrieve_limit: int`, `org_id: str`, `user_id: str`; classmethod `AmemConfig.from_env() -> AmemConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_config.py
import importlib
from memory.config import AmemConfig


def test_defaults_when_env_absent(monkeypatch):
    for k in ("AMEM_ENABLED", "AMEM_STORE_URL", "AMEM_VECTOR_SIZE",
              "AMEM_READ_TIMEOUT_S", "AMEM_RETRIEVE_LIMIT",
              "AMEM_ORG_ID", "AMEM_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    cfg = AmemConfig.from_env()
    assert cfg.enabled is True
    assert cfg.store_url == "http://127.0.0.1:6333"
    assert cfg.vector_size == 3072
    assert cfg.read_timeout_s == 1.5
    assert cfg.retrieve_limit == 6
    assert cfg.org_id == "amx"
    assert cfg.user_id == "amx_reviewer"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("AMEM_ENABLED", "0")
    monkeypatch.setenv("AMEM_STORE_URL", "http://qdrant:6333")
    monkeypatch.setenv("AMEM_VECTOR_SIZE", "1536")
    cfg = AmemConfig.from_env()
    assert cfg.enabled is False
    assert cfg.store_url == "http://qdrant:6333"
    assert cfg.vector_size == 1536
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.config'`.

- [ ] **Step 3: Write minimal implementation**

```python
# memory/__init__.py
"""AgenticSys ↔ Amem integration glue (config, factory, scope, IO helpers)."""
```

```python
# memory/config.py
from __future__ import annotations

import os
from dataclasses import dataclass

_FALSEY = {"0", "false", "no", "off", ""}


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in _FALSEY


@dataclass(frozen=True)
class AmemConfig:
    enabled: bool
    store_url: str
    collection_name: str
    vector_size: int
    read_timeout_s: float
    write_timeout_s: float
    retrieve_limit: int
    org_id: str
    user_id: str

    @classmethod
    def from_env(cls) -> "AmemConfig":
        return cls(
            enabled=_flag("AMEM_ENABLED", "1"),
            store_url=os.environ.get("AMEM_STORE_URL", "http://127.0.0.1:6333"),
            collection_name=os.environ.get("AMEM_COLLECTION_NAME", "amem_memories"),
            vector_size=int(os.environ.get("AMEM_VECTOR_SIZE", "3072")),
            read_timeout_s=float(os.environ.get("AMEM_READ_TIMEOUT_S", "1.5")),
            write_timeout_s=float(os.environ.get("AMEM_WRITE_TIMEOUT_S", "5.0")),
            retrieve_limit=int(os.environ.get("AMEM_RETRIEVE_LIMIT", "6")),
            org_id=os.environ.get("AMEM_ORG_ID", "amx"),
            user_id=os.environ.get("AMEM_USER_ID", "amx_reviewer"),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add memory/__init__.py memory/config.py tests/memory/test_config.py
git commit -m "feat(memory): AmemConfig env-driven settings"
```

---

## Task 2: `NullAmemManager` — no-op fallback

**Files:**
- Create: `memory/null_manager.py`
- Test: `tests/memory/test_null_manager.py`

**Interfaces:**
- Produces: `NullAmemManager` with attribute `enabled = False` and the Amem surface the glue uses: async `aadd_memory`, `arecord_tool_result`, `arecord_conversation`, `aupsert_case_memory`, `asearch_related`; sync `list_memories`, `delete_memory`, `get_memory`, `close`. Async writes return `None`; `asearch_related` and `list_memories` return `[]`; `delete_memory` returns `False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_null_manager.py
import asyncio
from memory.null_manager import NullAmemManager


def test_null_manager_surface():
    m = NullAmemManager()
    assert m.enabled is False

    async def go():
        assert await m.aadd_memory(level="working", content="x", scope=None) is None
        assert await m.arecord_conversation(raw_question="q", raw_answer="a", scope=None) is None
        assert await m.aupsert_case_memory(scope=None) is None
        assert await m.asearch_related("q", scope=None) == []

    asyncio.run(go())
    assert m.list_memories(scope=None) == []
    assert m.delete_memory("id") is False
    assert m.get_memory("id") is None
    m.close()  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_null_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.null_manager'`.

- [ ] **Step 3: Write minimal implementation**

```python
# memory/null_manager.py
"""No-op Amem manager. Returned when Amem is disabled or the store is unreachable.

Implements exactly the surface the memory/* helpers call, so every seam can invoke
Amem unconditionally and fall back to today's behavior with zero branching.
"""
from __future__ import annotations

from typing import Any


class NullAmemManager:
    enabled = False

    async def aadd_memory(self, **kwargs: Any) -> None:
        return None

    async def arecord_tool_result(self, **kwargs: Any) -> None:
        return None

    async def arecord_conversation(self, **kwargs: Any) -> None:
        return None

    async def aupsert_case_memory(self, **kwargs: Any) -> None:
        return None

    async def asearch_related(self, query: str, **kwargs: Any) -> list:
        return []

    def list_memories(self, **kwargs: Any) -> list:
        return []

    def delete_memory(self, memory_id: str, **kwargs: Any) -> bool:
        return False

    def get_memory(self, memory_id: str, **kwargs: Any):
        return None

    def close(self) -> None:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_null_manager.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add memory/null_manager.py tests/memory/test_null_manager.py
git commit -m "feat(memory): NullAmemManager no-op fallback"
```

---

## Task 3: Scope helpers — `build_scope`, `base_metadata`, `kps_for_turn`

**Files:**
- Create: `memory/scope.py`
- Test: `tests/memory/test_scope.py`

**Interfaces:**
- Consumes: `AmemConfig` (Task 1); `Amem.MemoryScope`.
- Produces:
  - `build_scope(cfg: AmemConfig, case_id: str, *, turn_id: str | None = None, agent_id: str | None = None) -> MemoryScope`
  - `base_metadata(session_id: str | None) -> dict` → `{"session_id": session_id}` (omits key if `None`)
  - `kps_for_turn(specialist_kb: dict, turn_id: str) -> list[str]` → the `claim` strings of every KP whose `captured_at_turn == turn_id`, across all specialists, skipping blanks.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_scope.py
from memory.config import AmemConfig
from memory.scope import build_scope, base_metadata, kps_for_turn

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_build_scope_sets_constants_and_fields():
    s = build_scope(CFG, "case_1", turn_id="t9", agent_id="risk")
    assert s.org_id == "amx"
    assert s.user_id == "amx_reviewer"
    assert s.case_id == "case_1"
    assert s.turn_id == "t9"
    assert s.agent_id == "risk"


def test_build_scope_case_only_leaves_turn_agent_none():
    s = build_scope(CFG, "case_1")
    assert s.turn_id is None and s.agent_id is None


def test_base_metadata_omits_none():
    assert base_metadata("sess_1") == {"session_id": "sess_1"}
    assert base_metadata(None) == {}


def test_kps_for_turn_filters_by_captured_turn():
    kb = {
        "risk": [
            {"claim": "A", "captured_at_turn": "t1"},
            {"claim": "B", "captured_at_turn": "t2"},
            {"claim": "", "captured_at_turn": "t1"},   # blank skipped
        ],
        "fraud": [{"claim": "C", "captured_at_turn": "t1"}],
    }
    assert sorted(kps_for_turn(kb, "t1")) == ["A", "C"]
    assert kps_for_turn(kb, "t2") == ["B"]
    assert kps_for_turn(kb, "tX") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_scope.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.scope'`.

- [ ] **Step 3: Write minimal implementation**

```python
# memory/scope.py
from __future__ import annotations

from Amem import MemoryScope

from .config import AmemConfig


def build_scope(cfg: AmemConfig, case_id: str, *,
                turn_id: str | None = None,
                agent_id: str | None = None) -> MemoryScope:
    return MemoryScope(
        org_id=cfg.org_id,
        user_id=cfg.user_id,
        case_id=case_id,
        turn_id=turn_id,
        agent_id=agent_id,
    )


def base_metadata(session_id: str | None) -> dict:
    return {"session_id": session_id} if session_id else {}


def kps_for_turn(specialist_kb: dict, turn_id: str) -> list[str]:
    out: list[str] = []
    for kps in (specialist_kb or {}).values():
        for kp in kps or []:
            if kp.get("captured_at_turn") != turn_id:
                continue
            claim = (kp.get("claim") or "").strip()
            if claim:
                out.append(claim)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_scope.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add memory/scope.py tests/memory/test_scope.py
git commit -m "feat(memory): scope + metadata + per-turn KP helpers"
```

---

## Task 4: Shared `FakeAmem` test double

**Files:**
- Create: `tests/memory/__init__.py` (empty)
- Create: `tests/memory/_fake_amem.py`
- Test: `tests/memory/test_fake_amem.py`

**Interfaces:**
- Produces: `FakeAmem` — records calls and returns canned data, implementing the same surface as `NullAmemManager` plus call-capture lists `added`, `conversations`, `case_upserts`, `deleted`, and a settable `search_results: list` / `listed: list`. `enabled = True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_fake_amem.py
import asyncio
from tests.memory._fake_amem import FakeAmem, FakeRecord


def test_fake_captures_and_returns():
    fake = FakeAmem()
    fake.search_results = [FakeRecord(id="r1", content="hello", level="conversation")]

    async def go():
        await fake.aadd_memory(level="working", content="k", scope="s", kind="knowledge_point")
        await fake.arecord_conversation(raw_question="q", raw_answer="a", scope="s",
                                        atomic_facts=["f"])
        await fake.aupsert_case_memory(scope="s")
        res = await fake.asearch_related("hello", scope="s")
        return res

    res = asyncio.run(go())
    assert fake.added and fake.added[0]["kind"] == "knowledge_point"
    assert fake.conversations and fake.conversations[0]["atomic_facts"] == ["f"]
    assert fake.case_upserts == 1
    assert res[0].record.content == "hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_fake_amem.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.memory._fake_amem'`.

- [ ] **Step 3: Write minimal implementation**

```python
# tests/memory/__init__.py
```

```python
# tests/memory/_fake_amem.py
"""In-memory Amem double for unit tests (no Qdrant, no network)."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeRecord:
    id: str
    content: str
    level: str = "conversation"
    scope: Any = None
    kind: str = "memory"
    metadata: dict = field(default_factory=dict)

    @property
    def level_obj(self):
        return SimpleNamespace(value=self.level)


def _result(rec: FakeRecord, score: float = 1.0):
    # Mirrors Amem SearchResult: .record, .score, .reason
    record = SimpleNamespace(id=rec.id, content=rec.content,
                             level=SimpleNamespace(value=rec.level),
                             metadata=rec.metadata, scope=rec.scope, kind=rec.kind)
    return SimpleNamespace(record=record, score=score, reason="hybrid")


class FakeAmem:
    enabled = True

    def __init__(self) -> None:
        self.added: list[dict] = []
        self.conversations: list[dict] = []
        self.case_upserts: int = 0
        self.deleted: list[str] = []
        self.search_results: list[FakeRecord] = []
        self.listed: list[FakeRecord] = []
        self.closed = False

    async def aadd_memory(self, **kwargs: Any):
        self.added.append(kwargs)
        return SimpleNamespace(id=f"add_{len(self.added)}")

    async def arecord_tool_result(self, **kwargs: Any):
        self.added.append(kwargs)
        return SimpleNamespace(id=f"tool_{len(self.added)}")

    async def arecord_conversation(self, **kwargs: Any):
        self.conversations.append(kwargs)
        return SimpleNamespace(id=f"conv_{len(self.conversations)}")

    async def aupsert_case_memory(self, **kwargs: Any):
        self.case_upserts += 1
        return SimpleNamespace(id="case_1")

    async def asearch_related(self, query: str, **kwargs: Any):
        return [_result(r) for r in self.search_results]

    def list_memories(self, **kwargs: Any):
        return [_result(r).record for r in self.listed]

    def delete_memory(self, memory_id: str, **kwargs: Any) -> bool:
        self.deleted.append(memory_id)
        return True

    def get_memory(self, memory_id: str, **kwargs: Any):
        return None

    def close(self) -> None:
        self.closed = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_fake_amem.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/memory/__init__.py tests/memory/_fake_amem.py tests/memory/test_fake_amem.py
git commit -m "test(memory): shared FakeAmem double"
```

---

## Task 5: Writer helpers — working mirror, conversation, case consolidation

**Files:**
- Create: `memory/writer.py`
- Test: `tests/memory/test_writer.py`

**Interfaces:**
- Consumes: `AmemConfig`, `build_scope`, `base_metadata` (Tasks 1, 3); `FakeAmem` (Task 4).
- Produces (all async, all swallow exceptions and return `None`):
  - `mirror_kp_working(amem, cfg, kp_dict: dict, *, case_id, turn_id, agent_id, session_id) -> None`
  - `write_conversation(amem, cfg, *, question, answer, case_id, turn_id, session_id, atomic_facts: list[str]) -> None`
  - `consolidate_case(amem, cfg, *, case_id, session_id) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_writer.py
import asyncio
from memory.config import AmemConfig
from memory import writer
from tests.memory._fake_amem import FakeAmem

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_mirror_kp_working_writes_working_level():
    fake = FakeAmem()
    kp = {"topic": "tsr_trend", "claim": "TSR rose", "numbers": [{"x": 1}],
          "confidence": "high", "captured_at_turn": "t1"}
    asyncio.run(writer.mirror_kp_working(fake, CFG, kp, case_id="c1",
                                         turn_id="t1", agent_id="risk", session_id="s1"))
    assert len(fake.added) == 1
    call = fake.added[0]
    assert call["level"] == "working"
    assert call["content"] == "TSR rose"
    assert call["kind"] == "knowledge_point"
    assert call["metadata"]["topic"] == "tsr_trend"
    assert call["metadata"]["session_id"] == "s1"
    assert call["scope"].case_id == "c1" and call["scope"].turn_id == "t1"
    assert call["scope"].agent_id == "risk"


def test_write_conversation_passes_atomic_facts():
    fake = FakeAmem()
    asyncio.run(writer.write_conversation(
        fake, CFG, question="Why held?", answer="FICO threshold.",
        case_id="c1", turn_id="t1", session_id="s1", atomic_facts=["FICO < 620"]))
    conv = fake.conversations[0]
    assert conv["raw_question"] == "Why held?"
    assert conv["raw_answer"] == "FICO threshold."
    assert conv["atomic_facts"] == ["FICO < 620"]
    assert conv["scope"].agent_id == "orchestrator"
    assert conv["metadata"]["session_id"] == "s1"


def test_write_conversation_empty_facts_becomes_none():
    fake = FakeAmem()
    asyncio.run(writer.write_conversation(
        fake, CFG, question="q", answer="a", case_id="c1", turn_id="t1",
        session_id="s1", atomic_facts=[]))
    assert fake.conversations[0]["atomic_facts"] is None


def test_consolidate_case_calls_upsert():
    fake = FakeAmem()
    asyncio.run(writer.consolidate_case(fake, CFG, case_id="c1", session_id="s1"))
    assert fake.case_upserts == 1


def test_writer_swallows_errors():
    class Boom(FakeAmem):
        async def aadd_memory(self, **k):
            raise RuntimeError("qdrant down")
    fake = Boom()
    # must NOT raise
    asyncio.run(writer.mirror_kp_working(fake, CFG, {"claim": "x"}, case_id="c1",
                                         turn_id="t1", agent_id="risk", session_id="s1"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_writer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.writer'`.

- [ ] **Step 3: Write minimal implementation**

```python
# memory/writer.py
"""Async, defensive Amem write helpers. Every function swallows exceptions and
returns None — an Amem write failure must never break a turn."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .config import AmemConfig
from .scope import base_metadata, build_scope


async def _guard(make_awaitable: Callable[[], Awaitable[Any]], timeout: float) -> Any:
    """Await make_awaitable() under a timeout, swallowing ALL exceptions —
    including any raised while BUILDING the awaitable. The construction runs
    inside the coroutine body (thus inside this try), so a malformed input
    (e.g. a None kp_dict) is swallowed too, not just Amem-call failures."""
    try:
        return await asyncio.wait_for(make_awaitable(), timeout=timeout)
    except Exception:
        return None


async def mirror_kp_working(amem, cfg: AmemConfig, kp_dict: dict, *,
                            case_id: str, turn_id: str, agent_id: str,
                            session_id: str) -> None:
    async def _do():
        metadata = base_metadata(session_id)
        metadata.update({
            "topic": kp_dict.get("topic"),
            "numbers": kp_dict.get("numbers"),
            "confidence": kp_dict.get("confidence"),
            "captured_at_turn": kp_dict.get("captured_at_turn"),
        })
        return await amem.aadd_memory(
            level="working",
            content=(kp_dict.get("claim") or ""),
            scope=build_scope(cfg, case_id, turn_id=turn_id, agent_id=agent_id),
            kind="knowledge_point",
            metadata=metadata,
        )
    await _guard(_do, cfg.write_timeout_s)


async def write_conversation(amem, cfg: AmemConfig, *, question: str, answer: str,
                             case_id: str, turn_id: str, session_id: str,
                             atomic_facts: list[str]) -> None:
    async def _do():
        return await amem.arecord_conversation(
            raw_question=question,
            raw_answer=answer,
            scope=build_scope(cfg, case_id, turn_id=turn_id, agent_id="orchestrator"),
            atomic_facts=(atomic_facts or None),
            metadata=base_metadata(session_id),
        )
    await _guard(_do, cfg.write_timeout_s)


async def consolidate_case(amem, cfg: AmemConfig, *, case_id: str,
                           session_id: str) -> None:
    async def _do():
        return await amem.aupsert_case_memory(
            scope=build_scope(cfg, case_id),
            metadata=base_metadata(session_id),
        )
    await _guard(_do, cfg.write_timeout_s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_writer.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add memory/writer.py tests/memory/test_writer.py
git commit -m "feat(memory): defensive async write helpers"
```

---

## Task 6: Reader helpers — `retrieve_context`, `search_kp`

**Files:**
- Create: `memory/reader.py`
- Test: `tests/memory/test_reader.py`

**Interfaces:**
- Consumes: `AmemConfig`, `build_scope` (Tasks 1, 3); `FakeAmem`/`FakeRecord` (Task 4).
- Produces (async, defensive, timeout → `""`/`None`):
  - `retrieve_context(amem, cfg, *, case_id, question) -> str` — searches `["working","conversation","case"]`, `include_working=True`, `search_mode="hybrid"`, `limit=cfg.retrieve_limit`. Returns a bracketed `[AMEM — …]` block with **full** untruncated `record.content` lines, most-relevant first; `""` when no results / disabled / error.
  - `search_kp(amem, cfg, *, case_id, topic) -> str | None` — hybrid search over `["working","conversation"]`, `limit=3`; returns the best `record.content` or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_reader.py
import asyncio
from memory.config import AmemConfig
from memory import reader
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")

LONG = "TSR breached the manual-review threshold in 2024-08 through 2024-10 " * 5


def test_retrieve_context_returns_full_untruncated_block():
    fake = FakeAmem()
    fake.search_results = [FakeRecord(id="r1", content=LONG, level="conversation")]
    block = asyncio.run(reader.retrieve_context(fake, CFG, case_id="c1", question="TSR?"))
    assert LONG in block                     # not clipped to 120/100 chars
    assert block.startswith("[AMEM")


def test_retrieve_context_empty_when_no_results():
    fake = FakeAmem()
    assert asyncio.run(reader.retrieve_context(fake, CFG, case_id="c1", question="q")) == ""


def test_retrieve_context_swallows_errors():
    class Boom(FakeAmem):
        async def asearch_related(self, q, **k):
            raise RuntimeError("down")
    assert asyncio.run(reader.retrieve_context(Boom(), CFG, case_id="c1", question="q")) == ""


def test_search_kp_returns_best_or_none():
    fake = FakeAmem()
    fake.search_results = [FakeRecord(id="r1", content="cached TSR value", level="working")]
    assert asyncio.run(reader.search_kp(fake, CFG, case_id="c1", topic="tsr")) == "cached TSR value"
    assert asyncio.run(reader.search_kp(FakeAmem(), CFG, case_id="c1", topic="tsr")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.reader'`.

- [ ] **Step 3: Write minimal implementation**

```python
# memory/reader.py
"""Async, defensive Amem read helpers. On disabled/empty/error they return the
empty result so callers fall back to today's warmth-hint + episodic behavior."""
from __future__ import annotations

import asyncio

from .config import AmemConfig
from .scope import build_scope

_LEVELS_CONTEXT = ["working", "conversation", "case"]
_LEVELS_KP = ["working", "conversation"]


async def retrieve_context(amem, cfg: AmemConfig, *, case_id: str, question: str) -> str:
    try:
        results = await asyncio.wait_for(
            amem.asearch_related(
                question,
                levels=_LEVELS_CONTEXT,
                scope=build_scope(cfg, case_id),
                search_mode="hybrid",
                limit=cfg.retrieve_limit,
                include_working=True,
            ),
            timeout=cfg.read_timeout_s,
        )
    except Exception:
        return ""
    if not results:
        return ""
    lines = []
    for r in results:
        rec = getattr(r, "record", None)
        if rec is None:
            continue
        level = getattr(getattr(rec, "level", None), "value", "memory")
        content = (getattr(rec, "content", "") or "").strip()
        if content:
            lines.append(f"  - [{level}] {content}")
    if not lines:
        return ""
    return (
        "[AMEM — relevant prior knowledge for this case (full claims, most "
        "relevant first). Use to avoid redundant queries and to anchor "
        "sub-questions:\n" + "\n".join(lines) + "\n]"
    )


async def search_kp(amem, cfg: AmemConfig, *, case_id: str, topic: str) -> str | None:
    try:
        results = await asyncio.wait_for(
            amem.asearch_related(
                topic,
                levels=_LEVELS_KP,
                scope=build_scope(cfg, case_id),
                search_mode="hybrid",
                limit=3,
                include_working=True,
            ),
            timeout=cfg.read_timeout_s,
        )
    except Exception:
        return None
    for r in results:
        rec = getattr(r, "record", None)
        content = (getattr(rec, "content", "") or "").strip() if rec else ""
        if content:
            return content
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_reader.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add memory/reader.py tests/memory/test_reader.py
git commit -m "feat(memory): defensive async read helpers (full-claim retrieval)"
```

---

## Task 7: Rewind + brief helpers

**Files:**
- Create: `memory/rewind.py`
- Create: `memory/brief.py`
- Test: `tests/memory/test_rewind_brief.py`

**Interfaces:**
- Consumes: `AmemConfig`, `build_scope` (Tasks 1, 3); `FakeAmem`/`FakeRecord` (Task 4).
- Produces:
  - `delete_turns(amem, cfg, *, case_id: str, turn_ids) -> int` (sync) — for each `turn_id`, `list_memories(scope=case+turn_id, include_working=True)` then `delete_memory(rec.id)`; returns count deleted; never raises.
  - `build_session_brief(amem, cfg, *, case_id: str) -> str` (sync) — returns the case memory `content` if one exists, else `f"Welcome to the discovery journey of case {case_id}."`; never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_rewind_brief.py
from memory.config import AmemConfig
from memory.rewind import delete_turns
from memory.brief import build_session_brief
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_delete_turns_deletes_each_listed_record():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="a", content="x"), FakeRecord(id="b", content="y")]
    n = delete_turns(fake, CFG, case_id="c1", turn_ids=["t1", "t2"])
    # 2 records listed per turn call * 2 turns = 4 deletes
    assert n == 4
    assert set(fake.deleted) == {"a", "b"}


def test_delete_turns_survives_errors():
    class Boom(FakeAmem):
        def list_memories(self, **k):
            raise RuntimeError("down")
    assert delete_turns(Boom(), CFG, case_id="c1", turn_ids=["t1"]) == 0


def test_brief_prefers_case_memory():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="case_1", content="Case summary: 3 spikes in TSR.",
                              level="case")]
    assert build_session_brief(fake, CFG, case_id="c1") == "Case summary: 3 spikes in TSR."


def test_brief_welcome_when_empty():
    assert build_session_brief(FakeAmem(), CFG, case_id="366132845011") == \
        "Welcome to the discovery journey of case 366132845011."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_rewind_brief.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.rewind'`.

- [ ] **Step 3: Write minimal implementation**

```python
# memory/rewind.py
"""Sync Amem delete-by-turn. Called from Flask rewind/cancel handlers. Deletes by
turn_id (a real scope field) — Amem cannot filter by session metadata."""
from __future__ import annotations

from .config import AmemConfig
from .scope import build_scope


def delete_turns(amem, cfg: AmemConfig, *, case_id: str, turn_ids) -> int:
    deleted = 0
    for turn_id in turn_ids or []:
        try:
            records = amem.list_memories(
                scope=build_scope(cfg, case_id, turn_id=turn_id),
                include_working=True,
            )
        except Exception:
            continue
        for rec in records:
            try:
                if amem.delete_memory(rec.id):
                    deleted += 1
            except Exception:
                continue
    return deleted
```

```python
# memory/brief.py
"""Sync session-start brief: the case summary if one exists, else a welcome line."""
from __future__ import annotations

from .config import AmemConfig
from .scope import build_scope


def build_session_brief(amem, cfg: AmemConfig, *, case_id: str) -> str:
    try:
        records = amem.list_memories(
            levels=["case"],
            scope=build_scope(cfg, case_id),
            kind="case_summary",   # aupsert_case_memory stores case memory as kind="case_summary"
            limit=1,
        )
    except Exception:
        records = []
    for rec in records:
        content = (getattr(rec, "content", "") or "").strip()
        if content:
            return content
    return f"Welcome to the discovery journey of case {case_id}."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_rewind_brief.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add memory/rewind.py memory/brief.py tests/memory/test_rewind_brief.py
git commit -m "feat(memory): delete-by-turn rewind + session brief helpers"
```

---

## Task 8: Backend-aware factory + health check

**Files:**
- Create: `memory/factory.py`
- Modify: `memory/__init__.py` (re-export public API)
- Test: `tests/memory/test_factory.py`

**Interfaces:**
- Consumes: `AmemConfig` (Task 1), `NullAmemManager` (Task 2); Amem `create_openai_manager`/`create_safechain_manager`.
- Produces: `build_amem_manager(cfg: AmemConfig, *, backend: str, logger=None) -> AmemManager | NullAmemManager`.
  - `cfg.enabled is False` → `NullAmemManager`.
  - `backend == "safechain"` → `create_safechain_manager(cfg.store_url, config=SafeChainConfig(dimensions=cfg.vector_size))`.
  - else (openai) → `create_openai_manager(cfg.store_url, config=OpenAIConfig(dimensions=cfg.vector_size))` (the manager builds its own `AsyncOpenAI`; memory content is redacted upstream).
  - Health check: `manager.list_memories(limit=1)` (cheap store round-trip). Any exception during build or health check → log + `NullAmemManager`.
- `memory/__init__.py` re-exports: `AmemConfig`, `build_amem_manager`, `NullAmemManager`, `build_scope`, `base_metadata`, `kps_for_turn`, and the writer/reader/rewind/brief functions.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_factory.py
from memory.config import AmemConfig
from memory.factory import build_amem_manager
from memory.null_manager import NullAmemManager

BASE = dict(store_url="http://127.0.0.1:6333", collection_name="c", vector_size=3072,
            read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
            org_id="amx", user_id="amx_reviewer")


def test_disabled_returns_null():
    cfg = AmemConfig(enabled=False, **BASE)
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, NullAmemManager)


def test_build_failure_falls_back_to_null(monkeypatch):
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    def boom(*a, **k):
        raise RuntimeError("qdrant unreachable")
    monkeypatch.setattr(f, "create_openai_manager", boom)
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, NullAmemManager)


def test_healthcheck_failure_falls_back(monkeypatch):
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    class HalfDead:
        def list_memories(self, **k):
            raise RuntimeError("store down")
        def close(self):
            pass
    monkeypatch.setattr(f, "create_openai_manager", lambda *a, **k: HalfDead())
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, NullAmemManager)


def test_healthy_manager_passthrough(monkeypatch):
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    class Healthy:
        def list_memories(self, **k):
            return []
    monkeypatch.setattr(f, "create_openai_manager", lambda *a, **k: Healthy())
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, Healthy)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_factory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.factory'`.

- [ ] **Step 3: Write minimal implementation**

```python
# memory/factory.py
"""Backend-aware AmemManager construction with a store health check. Any failure
(build error, unreachable Qdrant) degrades to NullAmemManager so the app runs
exactly as it does today."""
from __future__ import annotations

from .config import AmemConfig
from .null_manager import NullAmemManager

# Imported at module level so tests can monkeypatch them.
try:  # pragma: no cover - import guard
    from Amem.integrations import (
        OpenAIConfig,
        SafeChainConfig,
        create_openai_manager,
        create_safechain_manager,
    )
except Exception:  # pragma: no cover
    OpenAIConfig = SafeChainConfig = None
    create_openai_manager = create_safechain_manager = None


def _log(logger, event: str, payload: dict) -> None:
    if logger is not None:
        try:
            logger.log(event, payload)
        except Exception:
            pass


def build_amem_manager(cfg: AmemConfig, *, backend: str, logger=None):
    if not cfg.enabled:
        _log(logger, "amem_disabled", {"reason": "AMEM_ENABLED=0"})
        return NullAmemManager()
    try:
        if backend == "safechain":
            manager = create_safechain_manager(
                cfg.store_url, config=SafeChainConfig(dimensions=cfg.vector_size))
        else:
            manager = create_openai_manager(
                cfg.store_url, config=OpenAIConfig(dimensions=cfg.vector_size))
        # Health check: a cheap round-trip to the store.
        manager.list_memories(limit=1)
        _log(logger, "amem_ready", {"backend": backend, "store_url": cfg.store_url})
        return manager
    except Exception as exc:
        _log(logger, "amem_unavailable",
             {"backend": backend, "store_url": cfg.store_url, "error": repr(exc)})
        return NullAmemManager()
```

- [ ] **Step 4: Extend `memory/__init__.py`**

```python
# memory/__init__.py
"""AgenticSys ↔ Amem integration glue (config, factory, scope, IO helpers)."""
from .brief import build_session_brief
from .config import AmemConfig
from .factory import build_amem_manager
from .null_manager import NullAmemManager
from .reader import retrieve_context, search_kp
from .rewind import delete_turns
from .scope import base_metadata, build_scope, kps_for_turn
from .writer import consolidate_case, mirror_kp_working, write_conversation

__all__ = [
    "AmemConfig", "build_amem_manager", "NullAmemManager",
    "build_scope", "base_metadata", "kps_for_turn",
    "mirror_kp_working", "write_conversation", "consolidate_case",
    "retrieve_context", "search_kp", "delete_turns", "build_session_brief",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/ -v`
Expected: PASS (all memory unit tests green).

- [ ] **Step 6: Commit**

```bash
git add memory/factory.py memory/__init__.py tests/memory/test_factory.py
git commit -m "feat(memory): backend-aware factory + health check + package exports"
```

---

## Task 9: Add `qdrant-client` dependency

**Files:**
- Modify: `requirements.txt`

**Interfaces:** none (dependency only).

- [ ] **Step 1: Add the dependency line**

Append to `requirements.txt`:

```
qdrant-client>=1.7
```

- [ ] **Step 2: Install into the dev virtualenv**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pip install "qdrant-client>=1.7"`
Expected: `Successfully installed qdrant-client-...`.

- [ ] **Step 3: Verify import**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import qdrant_client; print(qdrant_client.__version__)"`
Expected: a version string prints, no error.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "build: add qdrant-client for Amem integration"
```

---

## Task 10: `AppContext` carries the Amem handle + scope

**Files:**
- Modify: `models/app_context.py` (add fields after `_episodic_records`, currently line 84)
- Test: `tests/memory/test_app_context_amem_fields.py`

**Interfaces:**
- Produces: `AppContext` gains `_amem: Any = None`, `_amem_cfg: Any = None`, `_case_id: str | None = None`, `_session_id: str | None = None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_app_context_amem_fields.py
from pathlib import Path
from models.app_context import AppContext


def test_app_context_has_amem_fields():
    ctx = AppContext(gateway=None, case_folder=Path("."), logger=None)
    assert ctx._amem is None
    assert ctx._amem_cfg is None
    assert ctx._case_id is None
    assert ctx._session_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_app_context_amem_fields.py -v`
Expected: FAIL with `AttributeError: 'AppContext' object has no attribute '_amem'`.

- [ ] **Step 3: Add the fields**

In `models/app_context.py`, immediately after the `_episodic_records` field (line 84), add:

```python
    # ── Amem integration (set in conductor._assemble_input) ──────────────
    _amem: Any = None                    # AmemManager or NullAmemManager
    _amem_cfg: Any = None                # memory.AmemConfig
    _case_id: str | None = None          # sess.case_id, for scope building
    _session_id: str | None = None       # sess.session_id, for Amem metadata
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_app_context_amem_fields.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add models/app_context.py tests/memory/test_app_context_amem_fields.py
git commit -m "feat(memory): AppContext carries Amem handle + scope"
```

---

## Task 11: Build the manager at server bootstrap; add `session_id`/`current_turn_id`

**Files:**
- Modify: `server.py` — imports; `CaseSession` fields (after line 208); bootstrap after `_CHAT_LLM` (line 270); set `session_id` in `_get_or_create_session` (after line 549); set `current_turn_id` in `_spawn_turn._runner`.
- Test: `tests/memory/test_server_wiring.py`

**Interfaces:**
- Consumes: `build_amem_manager`, `AmemConfig` (Task 8).
- Produces: module globals `_AMEM_CFG`, `_AMEM`; `CaseSession.session_id: str`, `CaseSession.current_turn_id: str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_server_wiring.py
import server


def test_server_exposes_amem_globals():
    assert hasattr(server, "_AMEM")
    assert hasattr(server, "_AMEM_CFG")
    # In dev with no Qdrant running, the health check fails → NullAmemManager.
    from memory.null_manager import NullAmemManager
    from Amem.core.manager import AmemManager
    assert isinstance(server._AMEM, (NullAmemManager, AmemManager))


def test_case_session_has_session_fields():
    from dataclasses import fields
    names = {f.name for f in fields(server.CaseSession)}
    assert "session_id" in names
    assert "current_turn_id" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_server_wiring.py -v`
Expected: FAIL (`AttributeError: module 'server' has no attribute '_AMEM'`).

- [ ] **Step 3: Add the import**

Near the other local imports at the top of `server.py`, add:

```python
from memory import AmemConfig, build_amem_manager
```

- [ ] **Step 4: Add `CaseSession` fields**

In `server.py`, immediately after the `specialist_kb` field (line 208), add:

```python
    # Amem session identity (metadata only; reads stay case-scoped/cross-session).
    # Rotated on full rewind / clear-history so prior sessions become immutable.
    session_id: str = ""
    # turn_id of the in-flight turn, for cancel-turn Amem cleanup.
    current_turn_id: str | None = None
```

- [ ] **Step 5: Build the manager at bootstrap**

In `server.py`, immediately after `_CHAT_LLM = FirewalledChatShim(_CLIENTS)` (line 270), add:

```python
_AMEM_CFG = AmemConfig.from_env()
_AMEM = build_amem_manager(
    _AMEM_CFG,
    backend=(BACKEND or os.environ.get("LLM_BACKEND", "openai")),
    logger=_BOOT_LOGGER,
)
```

(If a `BACKEND` module global does not exist, use `os.environ.get("LLM_BACKEND", "openai")` alone — check how the existing bootstrap reads the backend near `build_session_clients` at line 269 and match it.)

- [ ] **Step 6: Set `session_id` when a session is created**

In `_get_or_create_session`, immediately before `SESSIONS[case_id] = sess` (line 550), add:

```python
        sess.session_id = case_logger.session_id
```

- [ ] **Step 7: Track the in-flight turn id**

In `_spawn_turn`'s `_runner` (where `sess.current_inflight` is set, server.py ~line 187 region), set the turn id alongside it. Find the line that assigns `sess.current_inflight = (loop, task)` and add immediately after it:

```python
            sess.current_turn_id = turn_id
```

And in that runner's `finally` (where `current_inflight` is cleared), add:

```python
            sess.current_turn_id = None
```

- [ ] **Step 8: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_server_wiring.py -v`
Expected: PASS. (Importing `server` triggers bootstrap; with no Qdrant, `_AMEM` is a `NullAmemManager`.)

- [ ] **Step 9: Commit**

```bash
git add server.py tests/memory/test_server_wiring.py
git commit -m "feat(memory): build Amem manager at bootstrap; session_id + current_turn_id"
```

---

## Task 12: Populate `AppContext` Amem fields in the conductor

**Files:**
- Modify: `runner/turn/conductor.py` — `_assemble_input`, the `AppContext(...)` construction (lines 504-514).
- Test: `tests/memory/test_conductor_ctx_amem.py`

**Interfaces:**
- Consumes: `server._AMEM`, `server._AMEM_CFG` (Task 11); `AppContext` Amem fields (Task 10).
- Produces: `ctx._amem`, `ctx._amem_cfg`, `ctx._case_id`, `ctx._session_id` populated per turn.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_conductor_ctx_amem.py
"""The AppContext built per turn must carry the Amem handle + scope so the
distiller and finalize seams can write, and _assemble_input can read."""
import inspect
import runner.turn.conductor as conductor


def test_assemble_input_sets_amem_fields_source():
    src = inspect.getsource(conductor.TurnRunner._assemble_input)
    # The construction must thread the Amem handle, config, case id, session id.
    assert "_amem=" in src
    assert "_amem_cfg=" in src
    assert "_case_id=" in src
    assert "_session_id=" in src
```

(Note: this is a source-level guard because building a full `TurnRunner` requires a live session. Task 17's integration test exercises the behavior end-to-end.)

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_conductor_ctx_amem.py -v`
Expected: FAIL (`assert "_amem=" in src`).

- [ ] **Step 3: Import the Amem globals in the conductor**

At the top of `runner/turn/conductor.py`, add:

```python
import server as _server_module  # for _AMEM / _AMEM_CFG (built at bootstrap)
```

(If importing `server` from the conductor risks a circular import, instead read them lazily inside `_assemble_input` via `from server import _AMEM, _AMEM_CFG` in a `try/except Exception:` that falls back to `NullAmemManager()` + `AmemConfig.from_env()`. Verify no circular import by running the test; prefer the top-level import if it works.)

- [ ] **Step 4: Thread the fields into `AppContext(...)`**

In `_assemble_input`, extend the `AppContext(...)` construction (lines 504-514) with four keyword args:

```python
        ctx = AppContext(
            gateway=sess.gateway,
            case_folder=case_folder,
            logger=sess.logger,
            _specialist_kb=sess.specialist_kb,
            _distiller=getattr(orchestrator, "distiller_agent", None),
            _turn_id=turn_id,
            _emit_event=self._emit_event,
            _node_trace_store=_NODE_TRACE_STORE,
            _catalog=sess.catalog,
            _amem=getattr(_server_module, "_AMEM", None),
            _amem_cfg=getattr(_server_module, "_AMEM_CFG", None),
            _case_id=sess.case_id,
            _session_id=sess.session_id,
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_conductor_ctx_amem.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add runner/turn/conductor.py tests/memory/test_conductor_ctx_amem.py
git commit -m "feat(memory): thread Amem handle + scope into per-turn AppContext"
```

---

## Task 13: Mirror KPs into Amem working layer at the distiller seam

**Files:**
- Modify: `agent_factories/agent_tools/distiller_pass.py` — after each `sess_list.append(kp_dict)` (the narrow-insert at ~line 147 and the LLM-path loop at ~line 293).
- Test: `tests/memory/test_distiller_mirror.py`

**Interfaces:**
- Consumes: `memory.mirror_kp_working` (Task 5); `app_ctx._amem`, `_amem_cfg`, `_case_id`, `_session_id`, `_turn_id` (Tasks 10, 12).
- Produces: a helper `_mirror_kp(app_ctx, name, kp_dict)` in `distiller_pass.py` that awaits `mirror_kp_working` when Amem is present.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_distiller_mirror.py
import asyncio
from types import SimpleNamespace
from tests.memory._fake_amem import FakeAmem
from memory.config import AmemConfig
import agent_factories.agent_tools.distiller_pass as dp

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_mirror_kp_writes_to_amem_working():
    fake = FakeAmem()
    app_ctx = SimpleNamespace(_amem=fake, _amem_cfg=CFG, _case_id="c1",
                              _session_id="s1", _turn_id="t1")
    kp = {"topic": "tsr", "claim": "TSR up", "captured_at_turn": "t1",
          "numbers": [], "confidence": "high"}
    asyncio.run(dp._mirror_kp(app_ctx, "risk", kp))
    assert len(fake.added) == 1
    assert fake.added[0]["level"] == "working"
    assert fake.added[0]["scope"].agent_id == "risk"


def test_mirror_kp_noop_without_amem():
    app_ctx = SimpleNamespace(_amem=None, _amem_cfg=None, _case_id=None,
                              _session_id=None, _turn_id=None)
    # must not raise
    asyncio.run(dp._mirror_kp(app_ctx, "risk", {"claim": "x"}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_distiller_mirror.py -v`
Expected: FAIL (`AttributeError: module ... has no attribute '_mirror_kp'`).

- [ ] **Step 3: Add the helper and call it**

At the top of `distiller_pass.py`, add the import:

```python
from memory import mirror_kp_working
```

Add the helper near the other module-level helpers:

```python
async def _mirror_kp(app_ctx, name: str, kp_dict: dict) -> None:
    """Best-effort mirror of one KP into Amem's working layer (semantic search
    coverage for in-turn/session KPs). No-op when Amem is absent."""
    amem = getattr(app_ctx, "_amem", None)
    cfg = getattr(app_ctx, "_amem_cfg", None)
    case_id = getattr(app_ctx, "_case_id", None)
    if amem is None or cfg is None or not case_id:
        return
    await mirror_kp_working(
        amem, cfg, kp_dict,
        case_id=case_id,
        turn_id=getattr(app_ctx, "_turn_id", "") or "",
        agent_id=name,
        session_id=getattr(app_ctx, "_session_id", "") or "",
    )
```

In the **narrow-insert** branch, immediately after `sess_list.append(kp_dict)` (~line 147), add:

```python
        await _mirror_kp(app_ctx, name, kp_dict)
```

In the **LLM-path loop**, immediately after `sess_list.append(kp_dict)` (~line 293), add:

```python
        await _mirror_kp(app_ctx, name, kp_dict)
```

(Both call sites are already inside the `async def _distill_and_persist` coroutine, so `await` is valid.)

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_distiller_mirror.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_factories/agent_tools/distiller_pass.py tests/memory/test_distiller_mirror.py
git commit -m "feat(memory): mirror distilled KPs into Amem working layer"
```

---

## Task 14: Conversation + case writes at turn finalize

**Files:**
- Modify: `runner/turn/conductor.py` — `_finalize`, at the end of the `if final_answer is not None and cache_key:` block, after `snapshot_session` (line 1388 region).
- Test: `tests/memory/test_finalize_writes.py`

**Interfaces:**
- Consumes: `memory.write_conversation`, `memory.consolidate_case` (Task 5), `memory.kps_for_turn` (Task 3); `ctx._amem`, `_amem_cfg`, `sess.session_id`.
- Produces: a helper method `TurnRunner._persist_to_amem(answer_text)` that writes conversation (with this turn's KPs as atomic_facts) then consolidates case; awaited at the end of `_finalize`'s cache block.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_finalize_writes.py
import asyncio
from types import SimpleNamespace
from tests.memory._fake_amem import FakeAmem
from memory.config import AmemConfig
from runner.turn.conductor import TurnRunner

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def _runner_stub(fake):
    r = TurnRunner.__new__(TurnRunner)          # bypass __init__
    r.turn_id = "t1"
    r.verdict = SimpleNamespace(redacted_question="Why held?")
    sess = SimpleNamespace(case_id="c1", session_id="s1",
                           specialist_kb={"risk": [
                               {"claim": "FICO < 620", "captured_at_turn": "t1"}]})
    r.sess = sess
    r.ctx = SimpleNamespace(_amem=fake, _amem_cfg=CFG)
    return r


def test_persist_writes_conversation_with_turn_kps_then_consolidates():
    fake = FakeAmem()
    r = _runner_stub(fake)
    asyncio.run(r._persist_to_amem("FICO threshold sent it to manual review."))
    assert fake.conversations[0]["raw_question"] == "Why held?"
    assert fake.conversations[0]["atomic_facts"] == ["FICO < 620"]
    assert fake.case_upserts == 1


def test_persist_noop_without_amem():
    r = _runner_stub(FakeAmem())
    r.ctx = SimpleNamespace(_amem=None, _amem_cfg=None)
    asyncio.run(r._persist_to_amem("x"))        # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_finalize_writes.py -v`
Expected: FAIL (`AttributeError: 'TurnRunner' object has no attribute '_persist_to_amem'`).

- [ ] **Step 3: Add the imports and helper method**

At the top of `runner/turn/conductor.py`, add:

```python
from memory import consolidate_case, kps_for_turn, write_conversation
```

Add the method to the `TurnRunner` class (near `_finalize`):

```python
    async def _persist_to_amem(self, answer_text: str) -> None:
        """Durable conversation + case writes. Best-effort; never raises."""
        ctx = self.ctx
        amem = getattr(ctx, "_amem", None)
        cfg = getattr(ctx, "_amem_cfg", None)
        if amem is None or cfg is None:
            return
        sess = self.sess
        facts = kps_for_turn(sess.specialist_kb, self.turn_id)
        await write_conversation(
            amem, cfg,
            question=self.verdict.redacted_question,
            answer=answer_text,
            case_id=sess.case_id,
            turn_id=self.turn_id,
            session_id=sess.session_id,
            atomic_facts=facts,
        )
        await consolidate_case(amem, cfg, case_id=sess.case_id,
                               session_id=sess.session_id)
```

- [ ] **Step 4: Call it at the end of the finalize cache block**

In `_finalize`, immediately after the `snapshot_session(...)` call (the end of the `if final_answer is not None and cache_key:` block, ~line 1388), add:

```python
            await self._persist_to_amem(answer_text)
```

(This runs after `turn_done` was emitted at line 1331, so it adds no user-perceived answer latency; it is bounded by the per-turn wall-clock fence and by each helper's `write_timeout_s`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_finalize_writes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add runner/turn/conductor.py tests/memory/test_finalize_writes.py
git commit -m "feat(memory): durable conversation + case writes at turn finalize"
```

---

## Task 15: Amem retrieval replaces the bulk KB-warmth dump

**Files:**
- Modify: `runner/turn/input_assembly.py` — `assemble_orchestrator_input` gains `amem_block: str = ""`; prefer it over `_format_kb_warmth_hint`.
- Modify: `runner/turn/conductor.py` — `_assemble_input` computes `amem_block` before calling `assemble_orchestrator_input`.
- Test: `tests/memory/test_input_assembly_amem.py`

**Interfaces:**
- Consumes: `memory.retrieve_context` (Task 6); `ctx._amem`, `_amem_cfg`, `_case_id`.
- Produces: `assemble_orchestrator_input(sess, verdict, ctx, amem_block: str = "") -> str`. When `amem_block` is non-empty it replaces the bulk warmth hint (episodic stays for coreference); when empty, today's warmth-hint path is used unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_input_assembly_amem.py
from types import SimpleNamespace
from runner.turn.input_assembly import assemble_orchestrator_input


def _sess():
    return SimpleNamespace(
        specialist_kb={"risk": [{"topic": "tsr", "claim": "x" * 500}]},
        qa_cache={},
        logger=SimpleNamespace(log=lambda *a, **k: None),
    )


def test_amem_block_replaces_warmth_hint():
    sess = _sess()
    verdict = SimpleNamespace(redacted_question="What about TSR?")
    ctx = SimpleNamespace(_turn_id="t1")
    amem = "[AMEM — relevant prior knowledge for this case:\n  - [conversation] full claim not truncated\n]"
    out = assemble_orchestrator_input(sess, verdict, ctx, amem_block=amem)
    assert "full claim not truncated" in out
    assert "KB-warmth" not in out          # bulk warmth hint suppressed
    assert "What about TSR?" in out


def test_no_amem_block_uses_legacy_warmth():
    sess = _sess()
    verdict = SimpleNamespace(redacted_question="What about TSR?")
    ctx = SimpleNamespace(_turn_id="t1")
    out = assemble_orchestrator_input(sess, verdict, ctx, amem_block="")
    assert "KB-warmth" in out               # legacy path intact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_input_assembly_amem.py -v`
Expected: FAIL (`TypeError: assemble_orchestrator_input() got an unexpected keyword argument 'amem_block'`).

- [ ] **Step 3: Update `assemble_orchestrator_input`**

Replace the current function body's signature and warmth/compose lines in `runner/turn/input_assembly.py` (lines 60-86) with:

```python
def assemble_orchestrator_input(sess, verdict, ctx, amem_block: str = "") -> str:
    """Build the orchestrator's framed user message and stash episodic records on ctx.

    When *amem_block* (Amem hybrid retrieval) is provided, it replaces the bulk
    KB-warmth dump — full-claim, relevance-ranked, no 120-char clip. Episodic is
    kept regardless (it resolves coreference against the immediate thread).
    Returns the framed question string. Side effect: sets ctx._episodic_records.
    """
    if amem_block:
        warmth_hint = ""
    else:
        warmth_hint = _format_kb_warmth_hint(sess.specialist_kb)
        if warmth_hint:
            sess.logger.log("kb_warmth_hint_emitted", {
                "turn_id": getattr(ctx, "_turn_id", None),
                "warm_specialists": [
                    {"name": n, "n_kps": len(kps)}
                    for n, kps in sess.specialist_kb.items() if kps
                ],
                "hint_length": len(warmth_hint),
            })
    try:
        episodic_window = build_records(sess.qa_cache)
        episodic_block = render_orchestrator_block(
            select_episodic(episodic_window, EPISODIC_TURNS))
    except Exception as _epi_exc:  # noqa: BLE001 — episodic assembly must never break a turn
        episodic_window, episodic_block = [], ""
        sess.logger.log("episodic_assembly_failed",
                        {"turn_id": getattr(ctx, "_turn_id", None),
                         "error": repr(_epi_exc)})
    ctx._episodic_records = episodic_window
    return _compose_framed_question(
        episodic_block, amem_block or warmth_hint, verdict.redacted_question)
```

- [ ] **Step 4: Compute `amem_block` in the conductor**

At the top of `runner/turn/conductor.py`, extend the memory import to include the reader:

```python
from memory import consolidate_case, kps_for_turn, retrieve_context, write_conversation
```

**`_assemble_input` is SYNC today** (`def _assemble_input(self) -> None:`) and is called exactly once — `self._assemble_input()` inside `async def run` (no other callers). To `await retrieve_context(...)`, make it async:
- Change `def _assemble_input(self) -> None:` → `async def _assemble_input(self) -> None:`.
- Change its single call site `self._assemble_input()` → `await self._assemble_input()`.

Then in `_assemble_input`, replace the `assemble_orchestrator_input` call with:

```python
        amem_block = ""
        _amem = getattr(ctx, "_amem", None)
        _amem_cfg = getattr(ctx, "_amem_cfg", None)
        if _amem is not None and _amem_cfg is not None and sess.case_id:
            amem_block = await retrieve_context(
                _amem, _amem_cfg, case_id=sess.case_id,
                question=self.verdict.redacted_question)
        framed_question = assemble_orchestrator_input(
            sess, self.verdict, ctx, amem_block=amem_block)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_input_assembly_amem.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Regression — existing episodic/input-assembly tests still green**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -k "episodic or input_assembly or warmth" -v`
Expected: PASS (no regressions in the legacy path).

- [ ] **Step 7: Commit**

```bash
git add runner/turn/input_assembly.py runner/turn/conductor.py tests/memory/test_input_assembly_amem.py
git commit -m "feat(memory): Amem retrieval replaces bulk KB-warmth dump (full-claim)"
```

---

## Task 16: `kb_lookup` semantic fallback + rewind/brief server wiring

**Files:**
- Modify: `tools/kb_tools.py` — `kb_lookup` async semantic fallback on exact-slug miss.
- Modify: `server.py` — rewind/cancel deletes; clear-history session rotation; session-brief emit; close on shutdown.
- Test: `tests/memory/test_kb_lookup_fallback.py`

**Interfaces:**
- Consumes: `memory.search_kp` (Task 6), `memory.delete_turns` (Task 7), `memory.build_session_brief` (Task 7); `app_ctx._amem/_amem_cfg/_case_id`; `server._AMEM/_AMEM_CFG`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_kb_lookup_fallback.py
import asyncio
from types import SimpleNamespace
from tests.memory._fake_amem import FakeAmem, FakeRecord
from memory.config import AmemConfig
import tools.kb_tools as kb

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_kb_lookup_semantic_fallback_on_slug_miss():
    fake = FakeAmem()
    fake.search_results = [FakeRecord(id="r1", content="TSR peaked at 0.42 in 2024-09")]
    app_ctx = SimpleNamespace(_specialist_kb={}, _amem=fake, _amem_cfg=CFG, _case_id="c1")
    ctx = SimpleNamespace(context=app_ctx)
    out = asyncio.run(kb.kb_lookup.on_invoke_tool(ctx, '{"topic": "tsr_peak"}'))
    assert "TSR peaked at 0.42" in out


def test_kb_lookup_still_reports_empty_when_no_amem_and_no_kb():
    app_ctx = SimpleNamespace(_specialist_kb={}, _amem=None, _amem_cfg=None, _case_id="c1")
    ctx = SimpleNamespace(context=app_ctx)
    out = asyncio.run(kb.kb_lookup.on_invoke_tool(ctx, '{"topic": "tsr_peak"}'))
    assert "not found" in out.lower() or "empty" in out.lower()
```

(Note: `function_tool`-decorated callables are invoked in tests via `.on_invoke_tool(ctx, json_args)`. If the installed `agents` version differs, adapt to call the underlying function directly — verify the attribute with `dir(kb.kb_lookup)`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_kb_lookup_fallback.py -v`
Expected: FAIL (returns "not found", no semantic fallback yet).

- [ ] **Step 3: Add the semantic fallback to `kb_lookup`**

In `tools/kb_tools.py`, add the import:

```python
from memory import search_kp
```

In `kb_lookup`, replace the final `return f"Topic '{topic}' not found in KB."` (line 129) with:

```python
    amem = getattr(app_ctx, "_amem", None)
    cfg = getattr(app_ctx, "_amem_cfg", None)
    case_id = getattr(app_ctx, "_case_id", None)
    if amem is not None and cfg is not None and case_id:
        hit = await search_kp(amem, cfg, case_id=case_id, topic=topic)
        if hit:
            return json.dumps({"topic": topic, "source": "amem_semantic",
                               "claim": hit}, default=str)
    return f"Topic '{topic}' not found in KB."
```

(`app_ctx` is already bound near the top of `kb_lookup` via `_get_kb(ctx)`; if only `kb` is in scope, add `app_ctx = ctx.context if ctx else None` at the point of use.)

- [ ] **Step 4: Wire rewind/cancel deletes and session rotation in `server.py`**

Add the import in `server.py`:

```python
from memory import build_session_brief, delete_turns
```

In `post_rewind` (server.py:961), in the **partial** branch, after the KB/qa_cache deletions (after line 999), add:

```python
        try:
            delete_turns(_AMEM, _AMEM_CFG, case_id=case_id, turn_ids=remove_turn_ids)
        except Exception:
            pass
```

In `post_rewind`'s **full** branch (the `else:` at line 1000, after `sess.specialist_kb.clear()` line 1008), add the session rotation (preserve prior Amem memory):

```python
        sess.session_id = f"case-{case_id}-{uuid.uuid4().hex[:6]}"
```

In `post_cancel_turn` (server.py:893), after the RAM clears (after line 934), add a delete of the in-flight turn's records:

```python
    if sess.current_turn_id:
        try:
            delete_turns(_AMEM, _AMEM_CFG, case_id=case_id,
                         turn_ids=[sess.current_turn_id])
        except Exception:
            pass
```

- [ ] **Step 5: Emit the session brief on session creation**

In `_get_or_create_session`, immediately after `sess.session_id = case_logger.session_id` (added in Task 11, before `SESSIONS[case_id] = sess`), add:

```python
        try:
            brief = build_session_brief(_AMEM, _AMEM_CFG, case_id=case_id)
            sess.emit("session_brief", {"case_id": case_id, "text": brief})
        except Exception:
            pass
```

(`sess.emit` buffers into `event_buffer`, so a client connecting after session open still receives it.)

- [ ] **Step 6: Close the manager on shutdown**

Find where the server registers shutdown/atexit (search `atexit` in `server.py`). If present, add `_AMEM.close()` to the handler. If none exists, add near the bottom of module setup:

```python
import atexit
atexit.register(lambda: _AMEM.close())
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/test_kb_lookup_fallback.py tests/memory/test_server_wiring.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add tools/kb_tools.py server.py tests/memory/test_kb_lookup_fallback.py
git commit -m "feat(memory): kb_lookup semantic fallback; rewind deletes; session rotation + brief"
```

---

## Task 17: End-to-end integration test (fake manager injected)

**Files:**
- Create: `tests/memory/test_integration_end_to_end.py`

**Interfaces:**
- Consumes: everything above; injects a `FakeAmem` as `server._AMEM` and drives one turn's assemble→finalize seams via the helper methods.

- [ ] **Step 1: Write the integration test**

```python
# tests/memory/test_integration_end_to_end.py
"""Exercises the write→read→rewind loop with a FakeAmem, proving the seams call
Amem with correct scope and that Amem-down (NullAmemManager) is inert."""
import asyncio
from types import SimpleNamespace
from tests.memory._fake_amem import FakeAmem, FakeRecord
from memory.config import AmemConfig
from memory import delete_turns, build_session_brief
from memory.reader import retrieve_context
from memory.writer import write_conversation, consolidate_case, mirror_kp_working
from memory.null_manager import NullAmemManager

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_full_loop_with_fake():
    fake = FakeAmem()

    async def turn():
        # distiller mirror
        await mirror_kp_working(fake, CFG, {"topic": "tsr", "claim": "TSR up",
                                            "captured_at_turn": "t1"},
                                case_id="c1", turn_id="t1", agent_id="risk",
                                session_id="s1")
        # finalize writes
        await write_conversation(fake, CFG, question="Why held?", answer="FICO.",
                                 case_id="c1", turn_id="t1", session_id="s1",
                                 atomic_facts=["FICO < 620"])
        await consolidate_case(fake, CFG, case_id="c1", session_id="s1")
        # next-turn retrieval
        fake.search_results = [FakeRecord(id="c1", content="TSR up (full claim)",
                                          level="working")]
        return await retrieve_context(fake, CFG, case_id="c1", question="TSR trend?")

    block = asyncio.run(turn())
    assert "TSR up (full claim)" in block
    assert fake.added and fake.conversations and fake.case_upserts == 1

    # rewind deletes turn t1
    fake.listed = [FakeRecord(id="c1", content="x")]
    assert delete_turns(fake, CFG, case_id="c1", turn_ids=["t1"]) == 1

    # brief prefers case memory
    fake.listed = [FakeRecord(id="case_1", content="Case: TSR breach", level="case")]
    assert build_session_brief(fake, CFG, case_id="c1") == "Case: TSR breach"


def test_null_manager_is_inert():
    null = NullAmemManager()

    async def go():
        await mirror_kp_working(null, CFG, {"claim": "x"}, case_id="c1", turn_id="t1",
                                agent_id="risk", session_id="s1")
        await write_conversation(null, CFG, question="q", answer="a", case_id="c1",
                                 turn_id="t1", session_id="s1", atomic_facts=[])
        return await retrieve_context(null, CFG, case_id="c1", question="q")

    assert asyncio.run(go()) == ""
    assert delete_turns(null, CFG, case_id="c1", turn_ids=["t1"]) == 0
    assert build_session_brief(null, CFG, case_id="c1").startswith("Welcome")
```

- [ ] **Step 2: Run the full memory suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/memory/ -v`
Expected: PASS (all tasks' tests green together).

- [ ] **Step 3: Full regression**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: PASS (or the same pre-existing failures as before this branch — no NEW failures). Investigate any new failure before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/memory/test_integration_end_to_end.py
git commit -m "test(memory): end-to-end write→read→rewind loop + null-manager inertness"
```

---

## Task 18: Live smoke test against a real Qdrant (manual, gated on Docker)

**Files:** none (operational verification).

**Interfaces:** exercises the real `create_openai_manager` → Qdrant path.

- [ ] **Step 1: Start Qdrant in Docker**

Run (requires Docker installed — not on PATH in the current dev shell; skip this task if Docker is unavailable and note it):

```bash
docker run -d --name amem-qdrant -p 6333:6333 -p 6334:6334 \
  -v "$PWD/.runtime/qdrant/storage:/qdrant/storage" qdrant/qdrant:v1.18.3
```

- [ ] **Step 2: Smoke a real manager round-trip**

Run (requires `OPENAI_API_KEY` in env):

```bash
AMEM_STORE_URL=http://127.0.0.1:6333 ~/.pyenv/versions/3.11.13/envs/autoAI/bin/python - <<'PY'
import asyncio
from memory import AmemConfig, build_amem_manager, write_conversation, retrieve_context
cfg = AmemConfig.from_env()
mgr = build_amem_manager(cfg, backend="openai")
print("manager:", type(mgr).__name__)   # expect AmemManager, not NullAmemManager
async def go():
    await write_conversation(mgr, cfg, question="Why was the payment held?",
                             answer="The FICO threshold sent it to manual review.",
                             case_id="smoke_case", turn_id="t1", session_id="s1",
                             atomic_facts=["FICO threshold triggered manual review"])
    block = await retrieve_context(mgr, cfg, case_id="smoke_case",
                                   question="FICO manual review")
    print("retrieved:", block[:200])
asyncio.run(go())
mgr.close()
PY
```

Expected: `manager: AmemManager` and a non-empty `retrieved:` block containing the stored claim.

- [ ] **Step 3: Tear down**

```bash
docker stop amem-qdrant && docker rm amem-qdrant
```

- [ ] **Step 4: Record the result**

Note in the PR/description whether the live smoke passed, or that it was skipped for lack of Docker. No commit (operational only).

---

## Self-Review

**Spec coverage** (spec §-by-§ → task):
- §1 principle / additive / failure-isolated → Tasks 2, 5, 6, 8 (null-object + defensive wrappers).
- §2 session shim (`session_id` metadata) → Tasks 3, 11 (mint), 16 (rotation).
- §3 scope mapping → Task 3.
- §4 writes (working KP / conversation+KPs / case) → Tasks 5, 13, 14.
- §5 rewind/clear semantics (delete-by-turn; clear-history rotates+preserves; cancel deletes turn) → Tasks 7, 16. **Spec correction:** deletes are by `turn_id` scope, not metadata (Amem has no metadata filter) — noted in Global Constraints and applied in Task 7/16.
- §6 dual env → Amem OpenAI provider already DONE; factory in Task 8; safechain branch stubbed in Task 8 (prod wiring of `Amem/integrations/safechain.py` remains, tracked in spec §16 / Risks).
- §7 reads (KP look-up + case-memory follow-up + kb_lookup fallback) → Tasks 6, 15, 16.
- §8 session brief → Tasks 7, 16.
- §9 dual env wiring → Task 8 (+ done provider).
- §10 infra/isolation (health check, timeouts, budgets) → Tasks 6, 8, 14.
- §11 prerequisites (qdrant-client; Docker-optional; in-memory tests) → Tasks 9, 18.
- §12 config env vars → Task 1.
- §13 testing → every task (TDD) + Task 17 integration.

**Placeholder scan:** no TBD/TODO; every code step shows complete code. The two "verify how the existing code reads X" notes (Task 11 backend global; Task 16 `function_tool` invocation) are explicit verification instructions with a concrete fallback, not deferred work.

**Type consistency:** `AmemConfig` fields, `build_scope`/`base_metadata`/`kps_for_turn` signatures, `mirror_kp_working`/`write_conversation`/`consolidate_case`/`retrieve_context`/`search_kp`/`delete_turns`/`build_session_brief`, and `FakeAmem`'s capture attributes are used identically across Tasks 3–17. `_amem`/`_amem_cfg`/`_case_id`/`_session_id` naming is consistent across AppContext (Task 10), conductor (Task 12), distiller (Task 13), finalize (Task 14), input-assembly (Task 15), kb_tools (Task 16).

**Known follow-ups (out of scope, tracked):** prod SafeChain factory wiring in `Amem/integrations/safechain.py` (needs prod questions per spec §15); full multi-session lifecycle (Phase 2); consolidation-frequency tuning.
```
