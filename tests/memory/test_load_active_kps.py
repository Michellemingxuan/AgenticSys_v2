"""Relevance-scoped subset load for large KBs (load_active_kps).

Triggered by the conductor only when a case exceeds ACTIVE_KP_THRESHOLD KPs;
here we test the loader in isolation against the Amem double.
"""
import asyncio
from types import SimpleNamespace

from tests.memory._fake_amem import FakeAmem, FakeRecord
from memory.config import AmemConfig
from memory.loader import load_active_kps

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def _rec(agent, topics, turn="t1"):
    return FakeRecord(
        id=agent + turn, content="",
        scope=SimpleNamespace(agent_id=agent, turn_id=turn),
        metadata={"knowledge_points": [{"topic": t} for t in topics]},
    )


def test_load_active_kps_builds_subset_and_skips_orchestrator():
    fake = FakeAmem()
    fake.search_results = [
        _rec("modeling", ["a", "b"]),
        _rec("bureau", ["c"]),
        _rec("orchestrator", ["skip"]),   # orchestrator record carries no specialist KPs
    ]
    out = asyncio.run(load_active_kps(fake, CFG, case_id="c1", question="how did tsr react"))
    assert out == {"modeling": [{"topic": "a"}, {"topic": "b"}],
                   "bureau": [{"topic": "c"}]}


def test_load_active_kps_caps_total_kps_at_limit():
    fake = FakeAmem()
    fake.search_results = [_rec("modeling", ["a", "b", "c"]), _rec("bureau", ["d", "e"])]
    out = asyncio.run(load_active_kps(fake, CFG, case_id="c1", question="q", limit=2))
    assert sum(len(v) for v in out.values()) == 2          # capped at the limit
    assert out == {"modeling": [{"topic": "a"}, {"topic": "b"}]}  # relevance order preserved


def test_load_active_kps_never_raises_returns_empty_on_error():
    class Boom(FakeAmem):
        async def asearch_related(self, query, **kwargs):
            raise RuntimeError("amem down")
    out = asyncio.run(load_active_kps(Boom(), CFG, case_id="c1", question="q"))
    assert out == {}          # caller then keeps the full load_case_kps result
