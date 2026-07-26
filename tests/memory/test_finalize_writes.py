"""Task 14: durable conversation + case writes at turn finalize."""
import asyncio
from types import SimpleNamespace

from tests.memory._fake_amem import FakeAmem
from memory.config import AmemConfig
from runner.turn.conductor import TurnRunner

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def _runner_stub(fake):
    r = TurnRunner.__new__(TurnRunner)          # bypass __init__
    r.turn_id = "t1"
    r.verdict = SimpleNamespace(redacted_question="Why held?")
    sess = SimpleNamespace(case_id="c1", session_id="s1",
                           specialist_kb={"risk": [
                               {"claim": "FICO < 620", "captured_at_turn": "t1"}]})
    r.sess = sess
    r.ctx = SimpleNamespace(_amem=fake, _amem_cfg=CFG)
    return r


def test_persist_writes_conversation_with_turn_kps_then_consolidates():
    fake = FakeAmem()
    r = _runner_stub(fake)
    asyncio.run(r._persist_to_amem("FICO threshold sent it to manual review."))
    assert fake.conversations[0]["raw_question"] == "Why held?"
    assert fake.conversations[0]["atomic_facts"] == ["FICO < 620"]
    assert fake.case_upserts == 1


def test_persist_noop_without_amem():
    r = _runner_stub(FakeAmem())
    r.ctx = SimpleNamespace(_amem=None, _amem_cfg=None)
    asyncio.run(r._persist_to_amem("x"))        # must not raise
