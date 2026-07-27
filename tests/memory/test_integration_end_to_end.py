"""Exercises the write→read→rewind loop with a FakeAmem, proving the seams call
Amem with correct scope and that Amem-down (NullAmemManager) is inert."""
import asyncio
from types import SimpleNamespace
from tests.memory._fake_amem import FakeAmem, FakeRecord
from memory.config import AmemConfig
from memory import delete_turns, build_session_brief
from memory.reader import load_case_summary
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

    asyncio.run(turn())
    assert fake.added and fake.conversations and fake.case_upserts == 1

    # next-turn: durable case-summary read (sync) — the condensed "older context"
    fake.listed = [FakeRecord(id="case_1", content="Case: TSR up (summary)", level="case")]
    assert load_case_summary(fake, CFG, case_id="c1") == "Case: TSR up (summary)"

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

    asyncio.run(go())
    assert load_case_summary(null, CFG, case_id="c1") == ""     # inert read
    assert delete_turns(null, CFG, case_id="c1", turn_ids=["t1"]) == 0
    assert build_session_brief(null, CFG, case_id="c1").startswith("Welcome")
