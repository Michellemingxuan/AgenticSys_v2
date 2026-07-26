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
