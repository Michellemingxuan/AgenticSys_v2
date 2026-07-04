# tests/test_server/test_plan_review_dispatch.py
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skills" / "workflow"


def test_team_construction_skill_content():
    body = (SKILLS / "team_construction.md").read_text(encoding="utf-8")
    # dispatch-shape guidance present
    assert "Dispatch shape" in body
    for shape in ("parallel", "collapse", "sequential"):
        assert shape in body.lower()
    # row-31 restriction removed
    assert "NOT TSR/CDSS" not in body


def test_orchestrator_instructions_vp_framing():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "agent_factories" / "orchestrator_agent.py").read_text(encoding="utf-8")
    assert "SINGLE response" not in src          # forced-parallel mandate removed
    assert "manager" in src.lower()              # VP framing present


# ── Task 6: server phased run — gating + dispatch-cap accessors ──────────────

@pytest.mark.asyncio
async def test_review_skipped_for_single_specialist(monkeypatch):
    import server
    calls = {"review": 0}

    async def fake_review(*a, **k):
        calls["review"] += 1
        return None

    monkeypatch.setattr(server, "_run_review", fake_review)
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments"}  # only 1
    assert server._is_multi_specialist_turn(ctx) is False


@pytest.mark.asyncio
async def test_review_runs_for_multi_specialist(monkeypatch):
    import server
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._domain_specialists_called = {"spend_payments", "modeling"}
    assert server._is_multi_specialist_turn(ctx) is True


def test_dispatch_cap_blocks_third_dispatch():
    import server
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._dispatch_count = 2
    assert server._dispatch_count(ctx) == 2
    # a re-dispatch request at count 2 must be refused by the caller (asserted
    # in the integration test); here we lock the accessor.


def test_dispatch_count_bump_clamps_at_two():
    import server
    ctx = server.AppContext.__new__(server.AppContext)
    ctx._dispatch_count = 0
    assert server._dispatch_count(ctx) == 0
    assert server._bump_dispatch_count(ctx) == 1
    assert server._bump_dispatch_count(ctx) == 2
    # cap: never exceeds 2 dispatch rounds per turn
    assert server._bump_dispatch_count(ctx) == 2
    assert server._dispatch_count(ctx) == 2


def test_is_multi_specialist_turn_missing_attr_is_false():
    import server
    ctx = server.AppContext.__new__(server.AppContext)
    # no _domain_specialists_called attribute set at all
    assert server._is_multi_specialist_turn(ctx) is False
