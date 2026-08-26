"""Per-turn ask times, read back from the trace.

`/history` used to send no timestamp at all, so a restored thread showed a
clock only on turns asked in that browser since the last history load — the
same conversation had times on some rows and not others for reasons invisible
to the reviewer. node_trace is the fix: it opens its first node when the turn
starts, so `MIN(started_at)` is a true ask time, and its rows outlive the
session that wrote them.
"""
from datetime import datetime, timezone
from pathlib import Path

from tools.node_trace import NodeTraceStore


def _node(store, *, case_id, turn_id, node, started_at):
    return store.insert(
        chat_id=f"case-{case_id}", case_id=case_id, turn_id=turn_id,
        node=node, parent_id=None, depth=0, started_at=started_at)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def test_returns_the_earliest_node_per_turn(tmp_path: Path) -> None:
    """The turn STARTED when its first node opened, not when the last one did."""
    store = NodeTraceStore(str(tmp_path / "t.db"))
    _node(store, case_id="C", turn_id="t1", node="screen",
          started_at="2026-05-21T10:00:00.000000+00:00")
    _node(store, case_id="C", turn_id="t1", node="synthesis",
          started_at="2026-05-21T10:00:42.000000+00:00")
    _node(store, case_id="C", turn_id="t2", node="screen",
          started_at="2026-05-21T11:30:00.000000+00:00")

    out = store.turn_started_at("C")
    assert out == {
        "t1": _epoch("2026-05-21T10:00:00+00:00"),
        "t2": _epoch("2026-05-21T11:30:00+00:00"),
    }


def test_scoped_to_one_case(tmp_path: Path) -> None:
    store = NodeTraceStore(str(tmp_path / "t.db"))
    _node(store, case_id="C", turn_id="mine", node="screen",
          started_at="2026-05-21T10:00:00+00:00")
    _node(store, case_id="OTHER", turn_id="theirs", node="screen",
          started_at="2026-05-21T10:00:00+00:00")

    assert list(store.turn_started_at("C")) == ["mine"]


def test_unknown_case_is_empty_not_an_error(tmp_path: Path) -> None:
    store = NodeTraceStore(str(tmp_path / "t.db"))
    assert store.turn_started_at("never-seen") == {}


def test_a_naive_timestamp_is_read_as_utc(tmp_path: Path) -> None:
    """Rows are written UTC-aware, but a hand-edited or older row need not be.
    Assuming local time would silently shift it by the server's offset."""
    store = NodeTraceStore(str(tmp_path / "t.db"))
    _node(store, case_id="C", turn_id="t", node="screen",
          started_at="2026-05-21T10:00:00")

    got = store.turn_started_at("C")["t"]
    assert got == datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc).timestamp()


def test_an_unparseable_timestamp_drops_only_that_turn(tmp_path: Path) -> None:
    """One bad row must not cost the whole thread its times."""
    store = NodeTraceStore(str(tmp_path / "t.db"))
    _node(store, case_id="C", turn_id="bad", node="screen", started_at="not-a-date")
    _node(store, case_id="C", turn_id="good", node="screen",
          started_at="2026-05-21T10:00:00+00:00")

    assert list(store.turn_started_at("C")) == ["good"]


def test_an_unreadable_db_yields_no_times_rather_than_raising(tmp_path: Path) -> None:
    """A history endpoint must degrade to undated rows, never to a 500."""
    store = NodeTraceStore(str(tmp_path / "t.db"))
    store._conn.close()   # simulate the db going away underneath us

    assert store.turn_started_at("C") == {}
