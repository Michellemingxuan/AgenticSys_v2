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
