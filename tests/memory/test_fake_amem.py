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
