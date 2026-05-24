from pathlib import Path

from tools.node_trace import NodeTraceStore
from tools.node_trace.optimization_report import (
    latency_section,
    memory_section,
    tokens_section,
)


def _seed(tmp_path: Path) -> Path:
    store = NodeTraceStore(str(tmp_path / "t.db"))
    parent = store.insert(
        chat_id="c", case_id="x", turn_id="T1",
        node="specialist.spend", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    store.update(parent, duration_ms=10_000, outcome="ok")
    for i, p_tok in enumerate([1000, 3000, 5000, 7000], start=1):
        r = store.insert(
            chat_id="c", case_id="x", turn_id="T1",
            node=f"specialist.spend.round_{i}",
            parent_id=parent, depth=1,
            started_at=f"2026-05-21T00:00:{i:02d}.000000+00:00",
        )
        store.update(
            r,
            duration_ms=2000,
            queue_wait_ms=200 if i == 4 else 10,
            llm_call_ms=1500,
            prompt_tokens=p_tok,
            completion_tokens=50,
            cached_input_tokens=200 if i > 1 else 0,
            cost_usd=p_tok * 0.15 / 1_000_000,
            outcome="ok",
        )
    return tmp_path / "t.db"


def test_memory_section_surfaces_growth(tmp_path: Path):
    db = _seed(tmp_path)
    out = memory_section(db)
    assert "specialist.spend" in out
    assert "7000" in out


def test_tokens_section_surfaces_cache_ratio(tmp_path: Path):
    db = _seed(tmp_path)
    out = tokens_section(db)
    assert "cache" in out.lower()
    assert "$" in out


def test_latency_section_surfaces_queue_wait(tmp_path: Path):
    db = _seed(tmp_path)
    out = latency_section(db)
    assert "queue" in out.lower()
