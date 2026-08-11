"""Tests for the one-time case-id repair migration.

The migration exists because normalizing case ids at ingress orphans every
record written before the normalizer landed: the system asks for
`"11854808010"` while old rows say `"11854808010 "`. These tests pin the two
properties that make a migration safe to run on real data — it must be a
DRY RUN unless asked, and it must be IDEMPOTENT.
"""
import json
import sqlite3

from tools.migrate_case_ids import (
    _repoint_prefix,
    migrate_node_trace,
    migrate_reports,
)


def _make_db(path, case_id):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE node_trace (chat_id TEXT, case_id TEXT, "
                 "turn_id TEXT, conversation_id TEXT)")
    conn.execute("CREATE TABLE session_snapshot (chat_id TEXT, case_id TEXT, "
                 "turn_id TEXT, conversation_id TEXT)")
    derived = f"{case_id}::amx_reviewer::credit_risk"
    for t in ("node_trace", "session_snapshot"):
        conn.execute(f"INSERT INTO {t} VALUES (?,?,?,?)",
                     (derived, case_id, "t1", derived))
    conn.commit()
    conn.close()


# ── derived-id repointing ───────────────────────────────────────────────────


def test_repoint_prefix_rewrites_only_the_leading_case_segment():
    """`conversation_id` is f"{case}::{user}::{pillar}", so the padded case id
    is baked into its prefix. Only that segment may change."""
    stale, clean = "11854808010 ", "11854808010"
    assert _repoint_prefix(f"{stale}::amx::credit_risk", stale, clean) == \
        "11854808010::amx::credit_risk"
    assert _repoint_prefix(stale, stale, clean) == clean
    # A different case that merely starts with the same digits is untouched.
    assert _repoint_prefix("118548080109::amx::x", stale, clean) == \
        "118548080109::amx::x"
    assert _repoint_prefix(None, stale, clean) is None


# ── node-trace ──────────────────────────────────────────────────────────────


def test_node_trace_dry_run_reports_but_writes_nothing(tmp_path):
    db = tmp_path / "traces.db"
    _make_db(db, "11854808010 ")

    notes = migrate_node_trace(str(db), apply=False)

    # The report names both spellings so the operator can see the scope.
    assert any("'11854808010 '" in n and "'11854808010'" in n for n in notes)
    conn = sqlite3.connect(db)
    left = conn.execute("SELECT case_id FROM node_trace").fetchone()[0]
    assert left == "11854808010 "     # untouched


def test_node_trace_apply_rewrites_case_and_derived_ids(tmp_path):
    db = tmp_path / "traces.db"
    _make_db(db, "11854808010 ")

    migrate_node_trace(str(db), apply=True)

    conn = sqlite3.connect(db)
    for t in ("node_trace", "session_snapshot"):
        cid, chat, conv = conn.execute(
            f"SELECT case_id, chat_id, conversation_id FROM {t}").fetchone()
        assert cid == "11854808010"
        # Derived ids move with it, or the migrated rows group under a
        # different conversation than everything written after them.
        assert chat == "11854808010::amx_reviewer::credit_risk"
        assert conv == "11854808010::amx_reviewer::credit_risk"


def test_node_trace_migration_is_idempotent(tmp_path):
    db = tmp_path / "traces.db"
    _make_db(db, "11854808010 ")

    migrate_node_trace(str(db), apply=True)
    second = migrate_node_trace(str(db), apply=True)

    assert any("already canonical" in n for n in second)


def test_node_trace_missing_db_is_not_an_error(tmp_path):
    notes = migrate_node_trace(str(tmp_path / "absent.db"), apply=True)
    assert any("nothing to do" in n for n in notes)


# ── reports/ ────────────────────────────────────────────────────────────────


def test_reports_merge_moves_files_into_the_clean_directory(tmp_path):
    padded = tmp_path / "11854808010 " / "charts"
    padded.mkdir(parents=True)
    (padded / "a.png").write_bytes(b"\x89PNG")
    clean = tmp_path / "11854808010"
    clean.mkdir()
    (clean / "summary.md").write_text("report")

    migrate_reports(tmp_path, apply=True)

    assert (clean / "charts" / "a.png").exists()
    assert (clean / "summary.md").read_text() == "report"   # not disturbed
    assert not (tmp_path / "11854808010 ").exists()          # emptied, removed


def test_reports_merge_never_overwrites_an_existing_file(tmp_path):
    """The reviewer's artifacts are not ours to silently replace: on a name
    collision the file already under the clean id wins."""
    padded = tmp_path / "C-1 "
    padded.mkdir()
    (padded / "summary.md").write_text("FROM PADDED")
    clean = tmp_path / "C-1"
    clean.mkdir()
    (clean / "summary.md").write_text("FROM CLEAN")

    notes = migrate_reports(tmp_path, apply=True)

    assert (clean / "summary.md").read_text() == "FROM CLEAN"
    assert (padded / "summary.md").read_text() == "FROM PADDED"  # kept, not lost
    assert any("already present and kept" in n for n in notes)


def test_reports_dry_run_moves_nothing(tmp_path):
    padded = tmp_path / "C-2 "
    padded.mkdir()
    (padded / "x.md").write_text("x")

    migrate_reports(tmp_path, apply=False)

    assert (padded / "x.md").exists()
    assert not (tmp_path / "C-2").exists()


# ── delete mode ─────────────────────────────────────────────────────────────


def test_delete_mode_drops_orphaned_trace_rows(tmp_path):
    """When the orphans are residue of a case the reviewer already cleared,
    repointing them would resurrect conversations into a thread meant to be
    empty. Delete mode removes them instead."""
    db = tmp_path / "traces.db"
    _make_db(db, "11854808010 ")

    notes = migrate_node_trace(str(db), apply=True, mode="delete")

    conn = sqlite3.connect(db)
    for t in ("node_trace", "session_snapshot"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert any("DELETE" in n for n in notes)


def test_delete_mode_leaves_canonical_rows_alone(tmp_path):
    """Only NON-canonical ids are orphans. A clean case sharing the db must
    survive — the migration is scoped by spelling, not by case."""
    db = tmp_path / "traces.db"
    _make_db(db, "11854808010 ")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO node_trace VALUES (?,?,?,?)",
                 ("366132845011::u::p", "366132845011", "t9", "366132845011::u::p"))
    conn.commit()
    conn.close()

    migrate_node_trace(str(db), apply=True, mode="delete")

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT case_id FROM node_trace").fetchall()
    assert rows == [("366132845011",)]


def test_delete_mode_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "traces.db"
    _make_db(db, "11854808010 ")

    migrate_node_trace(str(db), apply=False, mode="delete")

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM node_trace").fetchone()[0] == 1


def test_delete_mode_removes_the_padded_reports_directory(tmp_path):
    padded = tmp_path / "C-3 " / "charts"
    padded.mkdir(parents=True)
    (padded / "a.png").write_bytes(b"\x89PNG")
    clean = tmp_path / "C-3"
    clean.mkdir()
    (clean / "keep.md").write_text("curated")

    migrate_reports(tmp_path, apply=True, mode="delete")

    assert not (tmp_path / "C-3 ").exists()
    assert (clean / "keep.md").read_text() == "curated"   # clean id untouched
