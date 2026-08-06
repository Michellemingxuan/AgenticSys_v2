"""`captured_at_seq` — the sortable age marker that protects the newest KPs
from the relevance-only compaction.

Amem's hybrid search ranks on embedding + keyword similarity with NO recency
term, so replacing the working set with its result can discard exactly the KPs
the previous turn produced. These tests pin the three pieces that prevent it:
the marker itself, the union at compaction, and the chronology restored after a
relevance-ordered reload.
"""
import asyncio
import inspect
from types import SimpleNamespace

import runner.turn.conductor as conductor
from memory.config import AmemConfig
from memory.loader import (
    kp_seq,
    load_active_kps,
    max_kp_seq,
    merge_recent_kps,
)
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def _kp(topic, seq=None, turn="t1"):
    kp = {"topic": topic, "claim": f"claim for {topic}", "captured_at_turn": turn}
    if seq is not None:
        kp["captured_at_seq"] = seq
    return kp


# ── the marker ────────────────────────────────────────────────────────────

def test_kp_seq_treats_missing_and_junk_as_oldest():
    # Legacy KPs written before the field existed must sort oldest, matching
    # the `turn_seq, -1` convention in tools/episodic.py.
    assert kp_seq({"topic": "a"}) == -1
    assert kp_seq({"topic": "a", "captured_at_seq": None}) == -1
    assert kp_seq({"topic": "a", "captured_at_seq": "nonsense"}) == -1
    assert kp_seq({"topic": "a", "captured_at_seq": 7}) == 7
    assert kp_seq({"topic": "a", "captured_at_seq": "7"}) == 7   # JSON round-trip


def test_max_kp_seq_scans_all_agents_and_tolerates_empty():
    assert max_kp_seq({}) == -1
    assert max_kp_seq({"modeling": [], "bureau": []}) == -1
    assert max_kp_seq({"modeling": [_kp("a", 3)], "bureau": [_kp("b", 9), _kp("c", 4)]}) == 9


# ── the union at compaction ───────────────────────────────────────────────

def test_merge_keeps_newest_kps_the_relevance_search_dropped():
    # The scenario: 98 old KPs accumulated, last turn added 20 more (seq 5).
    # Amem's search returns 20 by similarity alone and misses every new one.
    previous = {"modeling": [_kp(f"old{i}", 1) for i in range(98)]
                            + [_kp(f"new{i}", 5) for i in range(20)]}
    compacted = {"modeling": [_kp(f"old{i}", 1) for i in range(20)]}

    merged = merge_recent_kps(compacted, previous, keep=20)

    topics = [kp["topic"] for kp in merged["modeling"]]
    assert sum(len(v) for v in merged.values()) == 40          # 20 relevance + 20 recent
    assert all(f"new{i}" in topics for i in range(20))         # nothing recent was lost


def test_merge_dedupes_recent_kps_the_search_already_returned():
    # Last turn's KPs ARE in Amem by now, so the search can legitimately
    # return them — they must not come back doubled.
    recent = [_kp("alpha", 5, turn="t5"), _kp("beta", 5, turn="t5")]
    previous = {"modeling": [_kp("old", 1, turn="t1")] + recent}
    compacted = {"modeling": [_kp("old", 1, turn="t1"), _kp("alpha", 5, turn="t5")]}

    merged = merge_recent_kps(compacted, previous, keep=20)

    topics = [kp["topic"] for kp in merged["modeling"]]
    assert topics.count("alpha") == 1
    assert sorted(topics) == ["alpha", "beta", "old"]


def test_merge_orders_each_agent_oldest_to_newest_for_supersession():
    # _active_kps resolves a repeated topic by taking the LAST entry, so a
    # merged list whose newest KP is not last would surface a stale claim.
    previous = {"modeling": [_kp("tsr", 2, turn="t2"), _kp("tsr", 7, turn="t7")]}
    compacted = {"modeling": [_kp("tsr", 7, turn="t7"), _kp("tsr", 2, turn="t2")]}

    merged = merge_recent_kps(compacted, previous, keep=20)

    from tools.kb_tools import _active_kps
    assert [kp["captured_at_seq"] for kp in merged["modeling"]] == [2, 7]
    assert [kp["captured_at_seq"] for kp in _active_kps(merged["modeling"])] == [7]


def test_merge_caps_the_protected_set_so_a_huge_turn_still_shrinks():
    # Without the cap, one turn emitting > K1 KPs would stay above threshold
    # forever: every turn re-runs the ~3s hybrid search and nothing shrinks.
    previous = {"modeling": [_kp(f"k{i}", 5) for i in range(300)]}
    compacted = {"bureau": [_kp("c", 1)]}

    merged = merge_recent_kps(compacted, previous, keep=20)

    assert sum(len(v) for v in merged.values()) == 21          # 1 relevance + 20 recent


def test_merge_with_no_previous_kps_is_the_bare_compacted_set():
    compacted = {"modeling": [_kp("a", 1)]}
    assert merge_recent_kps(compacted, {}, keep=20) == compacted
    assert merge_recent_kps(compacted, {"modeling": []}, keep=0) == compacted


# ── chronology after a relevance-ordered reload ───────────────────────────

def _rec(agent, kps, turn="t1"):
    return FakeRecord(
        id=agent + turn, content="",
        scope=SimpleNamespace(agent_id=agent, turn_id=turn),
        metadata={"knowledge_points": kps},
    )


def test_load_active_kps_reorders_relevance_results_by_age():
    # Search returns the seq-9 record first (most similar); rebuilding in
    # relevance order would leave the seq-2 KP last and let _active_kps treat
    # the superseded claim as current.
    fake = FakeAmem()
    fake.search_results = [
        _rec("modeling", [_kp("tsr", 9, turn="t9")], turn="t9"),
        _rec("modeling", [_kp("tsr", 2, turn="t2")], turn="t2"),
    ]
    out = asyncio.run(load_active_kps(fake, CFG, case_id="c1", question="q"))

    assert [kp["captured_at_seq"] for kp in out["modeling"]] == [2, 9]


def test_load_active_kps_still_caps_by_relevance_not_by_age():
    # Ordering is by age; SELECTION stays relevance-first. The seq-1 KP in the
    # second (less relevant) record must not displace a top-ranked one.
    fake = FakeAmem()
    fake.search_results = [
        _rec("modeling", [_kp("a", 8, turn="t8"), _kp("b", 8, turn="t8")], turn="t8"),
        _rec("bureau", [_kp("c", 1, turn="t1")], turn="t1"),
    ]
    out = asyncio.run(load_active_kps(fake, CFG, case_id="c1", question="q", limit=2))

    assert out == {"modeling": [_kp("a", 8, turn="t8"), _kp("b", 8, turn="t8")]}


# ── stamping at capture ───────────────────────────────────────────────────

def test_distiller_direct_insert_stamps_the_seq():
    # Every KP-creation site must stamp, or the KP is invisible to the recency
    # protection (seq -1 = oldest) and gets dropped at the next compaction.
    from agent_factories.agent_tools.distiller_pass import _distill_and_persist

    kb: dict = {}
    ctx = SimpleNamespace(
        logger=None, _distiller=object(), _specialist_kb=kb,
        _turn_id="t-9", _turn_seq=9, _node_trace_store=None,
    )
    narrow = SimpleNamespace(findings="3 accounts are 30+ DPD.", evidence=[])

    n = asyncio.run(_distill_and_persist(ctx, "bureau", "how many are 30+ DPD?", narrow))

    assert n == 1
    assert kb["bureau"][0]["captured_at_seq"] == 9
    assert kb["bureau"][0]["captured_at_turn"] == "t-9"


def test_make_chart_and_auto_chart_stamp_the_seq():
    import inspect as _inspect

    import agent_factories.agent_tools.auto_chart as auto_chart
    import tools.data_viz_tools as data_viz_tools

    for mod in (auto_chart, data_viz_tools):
        src = _inspect.getsource(mod)
        # Chart KPs are built as literal dicts, not through the distiller —
        # they need the stamp alongside `captured_at_turn`.
        assert src.count("captured_at_turn") == src.count("captured_at_seq"), (
            f"{mod.__name__} has a KP site stamping the turn id but not the seq")


# ── conductor wiring ──────────────────────────────────────────────────────

def test_assemble_input_stamps_turn_seq_and_reconciles_the_counter():
    src = inspect.getsource(conductor.TurnRunner._assemble_input)
    # KPs created this turn get the seq this turn will be assigned.
    assert "_turn_seq=" in src
    # _qa_turn_seq is per-SESSION but the KB is per-CASE: KPs seeded from Amem
    # can carry higher seqs from an earlier session and would otherwise pin the
    # OLDEST KPs as "newest".
    assert "max_kp_seq(kb)" in src
    # Compaction unions rather than replaces.
    assert "merge_recent_kps(" in src
    assert "kb = compacted" not in src
