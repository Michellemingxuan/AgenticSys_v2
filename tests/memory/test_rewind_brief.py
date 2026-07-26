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
