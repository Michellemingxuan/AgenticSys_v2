"""Durable per-specialist memory: write one conversation record per specialist
(KPs as atomic_facts + structured KPs + tool_calls in metadata), and reload the
specialist_kb dict from Amem via a case-scoped batched query."""
import asyncio
from types import SimpleNamespace

from memory.config import AmemConfig
from memory.writer import write_specialist_memory
from memory.loader import load_case_kps
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_write_specialist_memory_shape():
    fake = FakeAmem()
    kps = [{"topic": "tsr_trend", "claim": "TSR rose to 39.6",
            "numbers": [{"max": 39.6}], "confidence": "high", "captured_at_turn": "t1"}]
    tool_calls = [{"func": "summarize_trend",
                   "params": {"table": "model_scores", "column": "tot_struct_risk_score"}}]
    asyncio.run(write_specialist_memory(
        fake, CFG, case_id="c1", turn_id="t1", session_id="s1", agent_id="risk_specialist",
        sub_question="How did TSR react?", findings="TSR rose then fell.",
        kps=kps, tool_calls=tool_calls))
    conv = fake.conversations[0]
    assert conv["scope"].agent_id == "risk_specialist"          # per-specialist scope
    assert conv["raw_question"] == "How did TSR react?"
    assert conv["raw_answer"] == "TSR rose then fell."
    assert conv["atomic_facts"] == ["TSR rose to 39.6"]         # KP claims
    assert conv["metadata"]["knowledge_points"] == kps          # full structured KPs
    assert conv["metadata"]["tool_calls"] == tool_calls         # func+params, no payloads
    assert conv["metadata"]["session_id"] == "s1"


def test_write_specialist_memory_swallows_errors():
    class Boom(FakeAmem):
        async def arecord_conversation(self, **k):
            raise RuntimeError("down")
    # must not raise even with a malformed kp
    asyncio.run(write_specialist_memory(
        Boom(), CFG, case_id="c1", turn_id="t1", session_id="s1", agent_id="risk",
        sub_question="q", findings="a", kps=None, tool_calls=None))


def _rec(agent, kps, created):
    return FakeRecord(id=f"r-{created}", content="x", level="conversation",
                      scope=SimpleNamespace(agent_id=agent),
                      metadata={"knowledge_points": kps}, kind="qa_turn")


def test_load_case_kps_roundtrip_and_grouping():
    fake = FakeAmem()
    fake.listed = [
        _rec("risk", [{"topic": "tsr", "claim": "old", "captured_at_turn": "t1"}], "1"),
        _rec("fraud", [{"topic": "dev", "claim": "device X", "captured_at_turn": "t1"}], "1"),
        _rec("risk", [{"topic": "tsr", "claim": "new", "captured_at_turn": "t2"}], "2"),
    ]
    kb = load_case_kps(fake, CFG, case_id="c1")
    assert set(kb) == {"risk", "fraud"}                         # grouped by agent
    # chronological order preserved so "latest per topic" (new) is last
    assert [k["claim"] for k in kb["risk"]] == ["old", "new"]
    assert kb["fraud"][0]["claim"] == "device X"


def test_load_case_kps_skips_orchestrator_and_empty():
    fake = FakeAmem()
    fake.listed = [
        _rec("orchestrator", [{"topic": "x", "claim": "turn"}], "1"),   # skipped
        FakeRecord(id="r2", content="x", scope=SimpleNamespace(agent_id="risk"),
                   metadata={}, kind="qa_turn"),                        # no KPs → skipped
    ]
    assert load_case_kps(fake, CFG, case_id="c1") == {}


def test_load_case_kps_survives_errors():
    class Boom(FakeAmem):
        def list_memories(self, **k):
            raise RuntimeError("down")
    assert load_case_kps(Boom(), CFG, case_id="c1") == {}
