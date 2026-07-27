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


def test_write_conversation_empty_facts_stay_empty_list_not_none():
    # [] (not None) so Amem does NOT auto-summarize the Q&A into facts;
    # None would trigger an extra synthesis step + an "Atomic facts:" section.
    fake = FakeAmem()
    asyncio.run(writer.write_conversation(
        fake, CFG, question="q", answer="a", case_id="c1", turn_id="t1",
        session_id="s1", atomic_facts=[]))
    assert fake.conversations[0]["atomic_facts"] == []


def test_write_conversation_stores_team_dispatch_in_metadata():
    """Orchestrator record carries the round-1 team dispatch (which specialists
    were called with what sub-question + concepts), NOT the specialists' KPs."""
    fake = FakeAmem()
    dispatch = [{"specialist": "risk", "sub_question": "How risky?",
                 "concepts": ["fico", "dpd"]}]
    asyncio.run(writer.write_conversation(
        fake, CFG, question="q", answer="a", case_id="c1", turn_id="t1",
        session_id="s1", team_dispatch=dispatch))
    conv = fake.conversations[0]
    assert conv["metadata"]["team_dispatch"] == dispatch
    assert conv["metadata"]["session_id"] == "s1"
    assert conv["atomic_facts"] == []              # no specialist KPs, no auto-summarize
    assert conv["scope"].agent_id == "orchestrator"


def test_write_conversation_no_team_dispatch_key_when_absent():
    fake = FakeAmem()
    asyncio.run(writer.write_conversation(
        fake, CFG, question="q", answer="a", case_id="c1", turn_id="t1",
        session_id="s1"))
    assert "team_dispatch" not in fake.conversations[0]["metadata"]


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


def test_mirror_kp_working_swallows_malformed_kp():
    fake = FakeAmem()
    # A None (or non-dict) kp_dict must NOT raise — the guard catches the
    # construction-time AttributeError and the write is silently skipped.
    asyncio.run(writer.mirror_kp_working(fake, CFG, None, case_id="c1",
                                         turn_id="t1", agent_id="risk", session_id="s1"))
    assert fake.added == []
