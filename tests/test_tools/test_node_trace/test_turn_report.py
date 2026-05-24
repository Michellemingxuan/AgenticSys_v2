import csv
from pathlib import Path

from tools.node_trace import NodeTraceStore
from tools.node_trace.turn_report import _flatten, build_tree, render_text, write_csv


def _seed_db(tmp_path: Path) -> Path:
    store = NodeTraceStore(str(tmp_path / "t.db"))
    p = store.insert(
        chat_id="c", case_id="x", turn_id="T1",
        node="specialist.spend", parent_id=None, depth=0,
        started_at="2026-05-21T00:00:00.000000+00:00",
    )
    store.update(p, duration_ms=1000, outcome="ok")
    r = store.insert(
        chat_id="c", case_id="x", turn_id="T1",
        node="specialist.spend.round_1", parent_id=p, depth=1,
        started_at="2026-05-21T00:00:00.500000+00:00",
    )
    store.update(
        r,
        duration_ms=400,
        prompt_tokens=120,
        completion_tokens=8,
        total_tokens=128,
        outcome="ok",
    )
    return tmp_path / "t.db"


def test_build_tree_and_render(tmp_path: Path):
    db = _seed_db(tmp_path)
    tree = build_tree(db, chat_id="c", turn_id="T1")
    assert len(tree) == 1
    root = tree[0]
    assert root["node"] == "specialist.spend"
    assert len(root["children"]) == 1
    assert root["children"][0]["node"] == "specialist.spend.round_1"
    text = render_text(tree)
    assert "specialist.spend" in text
    assert "round_1" in text
    assert "120" in text  # prompt_tokens visible


def test_write_csv_exports_rows(tmp_path: Path):
    db = _seed_db(tmp_path)
    tree = build_tree(db, chat_id="c", turn_id="T1")
    out = tmp_path / "turn_T1.csv"
    written = write_csv(_flatten(tree), out)
    assert written == out
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2  # parent + 1 round
    assert rows[0]["node"] == "specialist.spend"
    assert rows[1]["node"] == "specialist.spend.round_1"
    assert rows[1]["prompt_tokens"] == "120"
