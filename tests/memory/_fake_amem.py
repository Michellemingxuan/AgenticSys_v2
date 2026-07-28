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
        self.case_upsert_calls: list[dict] = []
        self.deleted: list[str] = []
        self.search_results: list[FakeRecord] = []
        self.listed: list[FakeRecord] = []
        self.closed = False
        self.list_calls: list[dict] = []

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
        self.case_upsert_calls.append(kwargs)
        return SimpleNamespace(id="case_1")

    async def asearch_related(self, query: str, **kwargs: Any):
        return [_result(r) for r in self.search_results]

    def list_memories(self, **kwargs: Any):
        self.list_calls.append(kwargs)
        return [_result(r).record for r in self.listed]

    def delete_memory(self, memory_id: str, **kwargs: Any) -> bool:
        self.deleted.append(memory_id)
        return True

    def get_memory(self, memory_id: str, **kwargs: Any):
        return None

    def close(self) -> None:
        self.closed = True
