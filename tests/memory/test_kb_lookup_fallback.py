"""kb_lookup is a RAM-only cache: a slug hit returns the cached KP; a miss tells
the specialist to query the real data. There is NO Amem semantic fallback
(removed) — relevance-scoping happens once at load time (load_active_kps)."""
import asyncio
from types import SimpleNamespace

from tests.memory._fake_amem import FakeAmem, FakeRecord
from memory.config import AmemConfig
import tools.kb_tools as kb

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_kb_lookup_hits_ram_cache():
    app_ctx = SimpleNamespace(_specialist_kb={
        "risk": [{"topic": "tsr_peak", "claim": "TSR peaked at 39.6",
                  "numbers": [{"period": "2024-09", "value": 39.6}]}]})
    ctx = SimpleNamespace(context=app_ctx)
    out = asyncio.run(kb.kb_lookup.on_invoke_tool(ctx, '{"topic": "tsr_peak"}'))
    assert "TSR peaked at 39.6" in out
    assert "39.6" in out


def test_kb_lookup_miss_is_ram_only_no_amem_call():
    # Even with an Amem manager present, a slug miss does NOT hit Amem anymore.
    fake = FakeAmem()
    fake.search_results = [FakeRecord(id="r1", content="MUST NOT be returned")]
    app_ctx = SimpleNamespace(_specialist_kb={}, _amem=fake, _amem_cfg=CFG, _case_id="c1")
    ctx = SimpleNamespace(context=app_ctx)
    out = asyncio.run(kb.kb_lookup.on_invoke_tool(ctx, '{"topic": "tsr_peak"}'))
    assert "MUST NOT be returned" not in out
    assert "not found" in out.lower() or "empty" in out.lower()
