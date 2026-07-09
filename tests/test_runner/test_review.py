import types
from runner.turn.review import _is_multi_specialist_turn, _dispatch_count, _bump_dispatch_count


def test_multi_specialist_gate():
    ctx = types.SimpleNamespace(_domain_specialists_called={"a"})
    assert _is_multi_specialist_turn(ctx) is False
    ctx._domain_specialists_called = {"a", "b"}
    assert _is_multi_specialist_turn(ctx) is True


def test_dispatch_count_clamped_at_2():
    ctx = types.SimpleNamespace()
    assert _dispatch_count(ctx) == 0
    _bump_dispatch_count(ctx); _bump_dispatch_count(ctx); _bump_dispatch_count(ctx)
    assert _dispatch_count(ctx) == 2
