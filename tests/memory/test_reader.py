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
