"""`data_tools.case_scope` — per-turn gateway/catalog binding.

The module globals set by `init_tools` stay as the process-wide default (tests,
notebooks, main.py). A server turn binds its own case-scoped pair on top, and
every tool read resolves through `_gw()` / `_cat()`. The point is that two
turns on different cases, running at once, cannot re-point each other.
"""
import asyncio
import threading

from datalayer.gateway import LocalDataGateway
from tools import data_tools
from tools.data_tools import _cat, _gw, case_scope


def _gateway(case_id, fico):
    gw = LocalDataGateway(case_data={case_id: {"scores": [{"fico": fico}]}})
    gw.set_case(case_id)
    return gw


def test_unbound_reads_fall_back_to_the_init_tools_global(monkeypatch):
    """65 test call sites and main.py rely on `init_tools` alone — the scope is
    additive, never a precondition."""
    sentinel = _gateway("CASE-GLOBAL", 1)
    monkeypatch.setattr(data_tools, "_gateway", sentinel)
    assert _gw() is sentinel


def test_bound_scope_wins_over_the_global(monkeypatch):
    monkeypatch.setattr(data_tools, "_gateway", _gateway("CASE-GLOBAL", 1))
    scoped = _gateway("CASE-A", 700)
    with case_scope(scoped, "cat-a"):
        assert _gw() is scoped
        assert _cat() == "cat-a"


def test_scope_is_restored_even_when_the_turn_raises(monkeypatch):
    monkeypatch.setattr(data_tools, "_gateway", _gateway("CASE-GLOBAL", 1))
    before = _gw()
    try:
        with case_scope(_gateway("CASE-A", 700), None):
            raise RuntimeError("turn blew up")
    except RuntimeError:
        pass
    assert _gw() is before


def test_nested_scopes_unwind_to_the_outer_one():
    a, b = _gateway("CASE-A", 700), _gateway("CASE-B", 620)
    with case_scope(a, None):
        with case_scope(b, None):
            assert _gw() is b
        assert _gw() is a


def test_tasks_spawned_inside_a_turn_inherit_its_scope():
    """Specialists, distillers and auto-charts run as `asyncio.create_task`
    children. If they did not inherit the binding they would read the process
    global — i.e. some other case."""
    async def main():
        scoped = _gateway("CASE-A", 700)
        with case_scope(scoped, None):
            async def child():
                await asyncio.sleep(0)
                return _gw().query("scores")
            return await asyncio.gather(child(), child())

    assert asyncio.run(main()) == [[{"fico": 700}], [{"fico": 700}]]


def test_two_threads_hold_independent_scopes_simultaneously():
    """Each turn runs in its own thread with its own event loop
    (`server._spawn_turn`). The binding must not be visible across them."""
    seen: dict[str, object] = {}
    both_inside = threading.Barrier(2, timeout=5)

    def turn(case_id, fico):
        with case_scope(_gateway(case_id, fico), None):
            both_inside.wait()
            seen[case_id] = _gw().query("scores")
            both_inside.wait()

    ts = [threading.Thread(target=turn, args=a)
          for a in (("CASE-A", 700), ("CASE-B", 620))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)

    assert seen == {"CASE-A": [{"fico": 700}], "CASE-B": [{"fico": 620}]}


def test_a_thread_starts_unbound_so_the_scope_must_be_entered_inside_it():
    """Contexts are NOT inherited across `threading.Thread`. This pins why
    `_case_scope` is entered in `_run_turn_streamed` (which already runs in the
    turn's thread) rather than in the spawning code."""
    inner: list = []
    with case_scope(_gateway("CASE-A", 700), None):
        t = threading.Thread(target=lambda: inner.append(_gw()))
        t.start()
        t.join(timeout=5)
    assert inner[0] is data_tools._gateway     # the global, not the bound scope
