import asyncio
from dataclasses import replace

import pytest

from memory.config import AmemConfig
from memory import writer
from memory.writer import consolidate_case, write_conversation
from tests.memory._fake_amem import FakeAmem

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


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



# ── swallowed is not silent ─────────────────────────────────────────────────
#
# Reported from the private env: "memory is not stored into Amem". Nothing in
# the logs said why, because `_guard` caught every exception and returned None
# without a word — so every write failing forever looked exactly like every
# write succeeding. Never breaking a turn is right; being undiagnosable is not.

class _Logger:
    def __init__(self):
        self.events = []

    def log(self, evt, payload):
        self.events.append((evt, payload))


class _Boom:
    async def arecord_conversation(self, **_kw):
        raise RuntimeError("qdrant unreachable")

    async def aupsert_case_memory(self, **_kw):
        raise RuntimeError("qdrant unreachable")


class _Hangs:
    async def arecord_conversation(self, **_kw):
        await asyncio.sleep(10)

    async def aupsert_case_memory(self, **_kw):
        await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_a_failing_write_is_logged_with_its_cause():
    log = _Logger()
    cfg = AmemConfig.from_env()
    await write_conversation(_Boom(), cfg, question="q", answer="a",
                             case_id="C", turn_id="t", session_id="s",
                             logger=log)

    assert log.events, "a swallowed failure must still be reported"
    evt, payload = log.events[-1]
    assert evt == "amem_write_failed"
    assert payload["op"] == "write_conversation"
    assert payload["error_type"] == "RuntimeError"
    assert "qdrant unreachable" in payload["error"]


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_a_timeout_not_an_error():
    """The likeliest prod failure and the one with a different fix: a timeout
    means raise `AMEM_WRITE_TIMEOUT_S`, an error means fix the wiring."""
    log = _Logger()
    cfg = replace(AmemConfig.from_env(), write_timeout_s=0.01)
    await consolidate_case(_Hangs(), cfg, case_id="C", session_id="s", logger=log)

    evt, payload = log.events[-1]
    assert evt == "amem_write_timeout"
    assert payload["op"] == "consolidate_case"
    assert payload["timeout_s"] == 0.01


@pytest.mark.asyncio
async def test_writes_still_never_raise_and_stay_silent_when_they_succeed():
    class _OK:
        async def arecord_conversation(self, **_kw):
            return "ok"

    log = _Logger()
    cfg = AmemConfig.from_env()
    # No logger at all — must not blow up.
    await write_conversation(_Boom(), cfg, question="q", answer="a",
                             case_id="C", turn_id="t", session_id="s")
    # Success path logs nothing; the JSONL stays quiet on the happy path.
    await write_conversation(_OK(), cfg, question="q", answer="a",
                             case_id="C", turn_id="t", session_id="s", logger=log)
    assert log.events == []
