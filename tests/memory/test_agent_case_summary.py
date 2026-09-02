"""Per-specialist ("agent") case summaries: written gated on turn count,
isolated from the whole-case summary by kind, and injected into the specialist's
input."""
import asyncio
from types import SimpleNamespace

from tests.memory._fake_amem import FakeAmem, FakeRecord
from memory.config import AmemConfig
from memory import writer, reader
from agent_factories.agent_tools.specialist_input_tool import (
    _format_agent_case_summary_block, _compose_specialist_input,
    assemble_specialist_input,
)

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


# ── write: consolidate_agent_case ───────────────────────────────────────────

def test_consolidate_agent_case_builds_over_min_turns_with_agent_kind_scope():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id=f"r{i}", content="c") for i in range(4)]  # 4 turns
    asyncio.run(writer.consolidate_agent_case(
        fake, CFG, case_id="c1", agent_id="modeling", session_id="s1", min_turns=3))
    assert fake.case_upserts == 1
    call = fake.case_upsert_calls[0]
    assert call["kind"] == "agent_case_summary"          # isolated from whole-case
    assert call["scope"].agent_id == "modeling"          # agent-scoped
    # the turn count came from an agent-scoped conversation list
    count_call = [c for c in fake.list_calls if c.get("levels") == ["conversation"]][-1]
    assert count_call["scope"].agent_id == "modeling"


def test_consolidate_agent_case_skipped_at_or_under_min_turns():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="r0", content="c"), FakeRecord(id="r1", content="c")]  # 2
    asyncio.run(writer.consolidate_agent_case(
        fake, CFG, case_id="c1", agent_id="modeling", session_id="s1", min_turns=3))
    assert fake.case_upserts == 0                         # its episodic still covers it


# ── read: load_case_summary agent-scoped ────────────────────────────────────

def test_load_case_summary_agent_scoped_uses_kind_and_agent():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="a1", content="modeling's condensed findings", level="case")]
    out = reader.load_case_summary(fake, CFG, case_id="c1",
                                   agent_id="modeling", kind="agent_case_summary")
    assert out == "modeling's condensed findings"
    call = fake.list_calls[-1]
    assert call["kind"] == "agent_case_summary"
    assert call["scope"].agent_id == "modeling"


# ── compose / inject ────────────────────────────────────────────────────────

def test_agent_case_summary_block_and_compose_order():
    assert _format_agent_case_summary_block("") == ""
    b = _format_agent_case_summary_block("modeling saw TSR peak 39.6")
    assert "CASE SUMMARY" in b and "39.6" in b
    out = _compose_specialist_input("EPI", "KP", "the question", "DIR", case_summary_block=b)
    assert out.index("CASE SUMMARY") < out.index("EPI") < out.index("KP") \
        < out.index("DIR") < out.index("the question")
    # backward compatible: no case summary / directed -> unchanged shape
    assert _compose_specialist_input("EPI", "KP", "q") == "EPI\n\nKP\n\n--- New question ---\nq"


def test_assemble_specialist_input_injects_agent_case_summary():
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="a1", content="modeling's condensed findings", level="case")]
    app_ctx = SimpleNamespace(_specialist_kps={}, _episodic_records=[],
                              _amem=fake, _amem_cfg=CFG, _case_id="c1")
    out, _ = assemble_specialist_input(
        app_ctx, "modeling", "the sub-question", None, None, None, None)
    assert "CASE SUMMARY" in out and "condensed findings" in out
    assert "the sub-question" in out
    # report_agent has no agent case memory
    out2, _ = assemble_specialist_input(
        app_ctx, "report_agent", "draft the report", None, None, None, None)
    assert "CASE SUMMARY" not in out2
