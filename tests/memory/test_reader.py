"""load_case_summary — the sole Amem read helper (the semantic-read helpers
retrieve_context / search_kp were removed; relevance-scoping now happens at load
time via load_active_kps, and kp_lookup is RAM-only)."""
from memory.config import AmemConfig
from memory import reader
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_load_case_summary_returns_case_content():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="case_1", content="Case: TSR breached in Sep 2024",
                              level="case")]
    assert reader.load_case_summary(fake, CFG, case_id="c1") == \
        "Case: TSR breached in Sep 2024"


def test_load_case_summary_empty_when_no_summary():
    assert reader.load_case_summary(FakeAmem(), CFG, case_id="c1") == ""


def test_load_case_summary_swallows_errors():
    class Boom(FakeAmem):
        def list_memories(self, **k):
            raise RuntimeError("down")
    assert reader.load_case_summary(Boom(), CFG, case_id="c1") == ""
