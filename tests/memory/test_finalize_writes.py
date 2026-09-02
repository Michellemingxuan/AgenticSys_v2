"""Durable conversation + case writes at turn finalize.

Orchestrator record = question + team dispatch (round 1) + final answer
(round 2). The specialists' distilled KPs live on their OWN per-specialist
records, NOT duplicated onto the orchestrator record.
"""
import asyncio
from types import SimpleNamespace

from tests.memory._fake_amem import FakeAmem
from memory.config import AmemConfig
from runner.turn.conductor import TurnRunner

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def _runner_stub(fake, turn_seq=10):
    # turn_seq=10 → a consolidate cadence boundary (every 10th turn), so
    # consolidate_case fires; use an off-cadence value to assert it's skipped.
    r = TurnRunner.__new__(TurnRunner)          # bypass __init__
    r.turn_id = "t1"
    r.verdict = SimpleNamespace(redacted_question="Why held?")
    sess = SimpleNamespace(logger=SimpleNamespace(log=lambda *a, **k: None),
        case_id="c1", session_id="s1", _qa_turn_seq=turn_seq,
                           specialist_kps={"risk": [
                               {"claim": "FICO < 620", "captured_at_turn": "t1"}]})
    r.sess = sess
    r.ctx = SimpleNamespace(
        _amem=fake, _amem_cfg=CFG,
        _specialist_turn_records={"risk": {
            "sub_question": "How risky is the applicant?",
            "concepts": ["fico", "dpd"],
            "findings": "FICO below threshold.",
            "tool_calls": [{"func": "summarize_trend", "params": {}}],
        }})
    return r


def _orchestrator(fake):
    return [c for c in fake.conversations if c["scope"].agent_id == "orchestrator"][0]


def test_orchestrator_record_holds_team_dispatch_and_answer_not_specialist_kps():
    fake = FakeAmem()
    r = _runner_stub(fake)
    asyncio.run(r._persist_to_amem("FICO threshold sent it to manual review."))
    orch = _orchestrator(fake)
    assert orch["raw_question"] == "Why held?"
    assert orch["raw_answer"] == "FICO threshold sent it to manual review."
    # Round 1: team dispatch recorded (specialist + sub-question + concepts).
    assert orch["metadata"]["team_dispatch"] == [
        {"specialist": "risk", "sub_question": "How risky is the applicant?",
         "concepts": ["fico", "dpd"]}]
    # The specialists' KPs are NOT dumped onto the orchestrator record, and []
    # (not None) so Amem does not auto-summarize the answer into facts.
    assert orch["atomic_facts"] == []
    assert fake.case_upserts == 1


def test_persist_writes_per_specialist_record_with_its_subquestion():
    fake = FakeAmem()
    r = _runner_stub(fake)
    asyncio.run(r._persist_to_amem("answer"))
    spec = [c for c in fake.conversations if c["scope"].agent_id == "risk"][0]
    assert spec["raw_question"] == "How risky is the applicant?"


def test_consolidate_case_gated_to_cadence():
    # On-cadence (10th turn) → consolidate fires; off-cadence (7th) → skipped.
    on = FakeAmem()
    asyncio.run(_runner_stub(on, turn_seq=10)._persist_to_amem("a"))
    assert on.case_upserts == 1

    off = FakeAmem()
    asyncio.run(_runner_stub(off, turn_seq=7)._persist_to_amem("a"))
    assert off.case_upserts == 0                 # summary refreshed only every N turns
    assert off.conversations                     # per-turn conversation writes still happen


def test_persist_noop_without_amem():
    r = _runner_stub(FakeAmem())
    r.ctx = SimpleNamespace(_amem=None, _amem_cfg=None)
    asyncio.run(r._persist_to_amem("x"))        # must not raise
