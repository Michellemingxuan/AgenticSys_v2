import asyncio
from types import SimpleNamespace

from tests.memory._fake_amem import FakeAmem, FakeRecord
from memory.config import AmemConfig
import tools.kb_tools as kb

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def test_kb_lookup_semantic_fallback_on_slug_miss():
    fake = FakeAmem()
    fake.search_results = [FakeRecord(id="r1", content="TSR peaked at 0.42 in 2024-09")]
    app_ctx = SimpleNamespace(_specialist_kb={}, _amem=fake, _amem_cfg=CFG, _case_id="c1")
    ctx = SimpleNamespace(context=app_ctx)
    out = asyncio.run(kb.kb_lookup.on_invoke_tool(ctx, '{"topic": "tsr_peak"}'))
    assert "TSR peaked at 0.42" in out


def test_kb_lookup_still_reports_empty_when_no_amem_and_no_kb():
    app_ctx = SimpleNamespace(_specialist_kb={}, _amem=None, _amem_cfg=None, _case_id="c1")
    ctx = SimpleNamespace(context=app_ctx)
    out = asyncio.run(kb.kb_lookup.on_invoke_tool(ctx, '{"topic": "tsr_peak"}'))
    assert "not found" in out.lower() or "empty" in out.lower()
