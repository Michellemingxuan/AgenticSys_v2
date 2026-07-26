from memory.config import AmemConfig
from memory.rewind import delete_case_memory
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_delete_case_memory_deletes_all_listed():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="a", content="x"), FakeRecord(id="b", content="y")]
    n = delete_case_memory(fake, CFG, case_id="c1")
    assert n == 2
    assert set(fake.deleted) == {"a", "b"}


def test_delete_case_memory_scope_is_case_only():
    fake = FakeAmem()
    delete_case_memory(fake, CFG, case_id="c1")
    # list_memories called with a case-only scope + include_working
    assert fake.list_calls and fake.list_calls[-1]["scope"].case_id == "c1"
    assert fake.list_calls[-1]["scope"].turn_id is None
    assert fake.list_calls[-1].get("include_working") is True


def test_delete_case_memory_survives_errors():
    class Boom(FakeAmem):
        def list_memories(self, **k):
            raise RuntimeError("down")
    assert delete_case_memory(Boom(), CFG, case_id="c1") == 0
