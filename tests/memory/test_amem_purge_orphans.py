"""Leftover memory must be FINDABLE, not merely deletable.

`_known_cases()` reads this deployment's `reports/` folder, so it can only ever
name cases the CURRENT system has data for. A leftover — memory from a previous
system that redeployed against the same Amem — is by definition a case with no
folder, which made it invisible to `--list` and unreachable by `--purge --all`:
you could only clear it by already knowing the id and passing `--case`.

These tests pin the store-side enumeration that finds one, and the scope filter
that stops it reaching a colleague's memories in a shared Qdrant.
"""
import sys
from types import SimpleNamespace

from memory.config import AmemConfig
from tools import amem_purge

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


def _rec(case_id, *, org="amx", user="amx_reviewer"):
    return SimpleNamespace(
        scope=SimpleNamespace(org_id=org, user_id=user, case_id=case_id))


class _Store:
    """Honours limit/offset the way the real AmemManager does."""

    def __init__(self, records, *, fail=False):
        self.records = records
        self.fail = fail
        self.pages = 0

    def list_memories(self, *, limit=None, offset=0, scope=None, **kw):
        if self.fail:
            raise RuntimeError("store down")
        if scope is not None:  # per-case count
            return [r for r in self.records if r.scope.case_id == scope.case_id]
        self.pages += 1
        return self.records[offset:offset + (limit or len(self.records))]


# --------------------------------------------------------------------------
# _store_cases
# --------------------------------------------------------------------------

def test_counts_every_case_the_store_holds():
    store = _Store([_rec("aaa"), _rec("bbb"), _rec("aaa")])
    assert amem_purge._store_cases(store, CFG) == {"aaa": 2, "bbb": 1}


def test_another_scopes_records_are_not_ours_to_count():
    """A shared Qdrant may hold a colleague's memory under a different scope.
    Counting it would invite deleting it."""
    store = _Store([
        _rec("ours"),
        _rec("their_org", org="other_org"),
        _rec("their_user", user="other_reviewer"),
    ])
    assert amem_purge._store_cases(store, CFG) == {"ours": 1}


def test_pages_through_a_store_larger_than_one_page(monkeypatch):
    monkeypatch.setattr(amem_purge, "_PAGE", 2)
    store = _Store([_rec("a"), _rec("b"), _rec("c"), _rec("d"), _rec("e")])
    assert amem_purge._store_cases(store, CFG) == {
        "a": 1, "b": 1, "c": 1, "d": 1, "e": 1}
    assert store.pages == 3  # 2 + 2 + 1, the short page ends it


def test_a_failed_enumeration_is_none_not_empty(capsys):
    """`{}` would mean "the store answered and holds nothing" — i.e. no
    leftovers. A lookup that never completed must not be able to say that."""
    assert amem_purge._store_cases(_Store([], fail=True), CFG) is None
    assert "store enumeration failed" in capsys.readouterr().out


# --------------------------------------------------------------------------
# main() — the orphan is what the user actually sees
# --------------------------------------------------------------------------

def _wire(monkeypatch, store, known, argv, deleter=None):
    monkeypatch.setattr(amem_purge.AmemConfig, "from_env", classmethod(lambda cls: CFG))
    monkeypatch.setattr(amem_purge, "build_amem_manager",
                        lambda cfg, backend, logger=None: store)
    monkeypatch.setattr(amem_purge, "_known_cases", lambda: known)
    monkeypatch.setattr(amem_purge, "build_scope",
                        lambda cfg, case_id: SimpleNamespace(case_id=case_id))
    monkeypatch.setattr(amem_purge, "delete_case_memory",
                        deleter or (lambda *a, **k: SimpleNamespace(deleted=0, failed=0)))
    monkeypatch.setattr(sys, "argv", ["amem_purge.py", *argv])


def test_the_backend_comes_from_the_environment(monkeypatch, capsys):
    """The server builds its manager with `LLM_BACKEND`. A tool that hardcodes
    "openai" asks for a different manager than the one that wrote the
    memories — and in a safechain-only deployment cannot build one at all,
    which reported "Amem is unavailable" in exactly the environment where
    leftovers accumulate."""
    seen = {}
    store = _Store([_rec("111")])
    monkeypatch.setenv("LLM_BACKEND", "safechain")
    _wire(monkeypatch, store, known=["111"], argv=["--list"])
    monkeypatch.setattr(
        amem_purge, "build_amem_manager",
        lambda cfg, backend, logger=None: (seen.update(backend=backend), store)[1])

    amem_purge.main()
    assert seen["backend"] == "safechain"
    assert "backend     safechain" in capsys.readouterr().out


def test_an_explicit_backend_flag_wins(monkeypatch):
    seen = {}
    store = _Store([_rec("111")])
    monkeypatch.setenv("LLM_BACKEND", "safechain")
    _wire(monkeypatch, store, known=["111"], argv=["--list", "--backend", "openai"])
    monkeypatch.setattr(
        amem_purge, "build_amem_manager",
        lambda cfg, backend, logger=None: (seen.update(backend=backend), store)[1])

    amem_purge.main()
    assert seen["backend"] == "openai"


def test_an_unavailable_store_reports_which_cause(monkeypatch, capsys):
    """The factory swallows every failure into a NullAmemManager, so without
    the captured reason the tool could only guess between causes."""
    class _Null:
        pass
    _Null.__name__ = "NullAmemManager"

    def _build(cfg, backend, logger=None):
        if logger is not None:
            logger.log("amem_unavailable", {"backend": backend, "error": "boom"})
        return _Null()

    _wire(monkeypatch, _Store([]), known=["111"], argv=["--list"])
    monkeypatch.setattr(amem_purge, "build_amem_manager", _build)

    assert amem_purge.main() == 1
    out = capsys.readouterr().out
    assert "amem_unavailable" in out and "boom" in out


def test_a_case_with_no_reports_folder_is_flagged_as_an_orphan(monkeypatch, capsys):
    store = _Store([_rec("111"), _rec("999"), _rec("999")])
    _wire(monkeypatch, store, known=["111"], argv=["--list"])

    assert amem_purge.main() == 0
    out = capsys.readouterr().out
    assert "999" in out and "ORPHAN" in out
    # The known case is listed, and NOT accused of being a leftover.
    assert "111" in out
    assert out.count("ORPHAN") == 1
    assert "1 orphan case(s)" in out


def test_purge_all_reaches_an_orphan(monkeypatch, capsys):
    """The whole point: `--all` used to mean "every case in reports/", which
    could never include the one case that needed clearing."""
    purged = []
    store = _Store([_rec("111"), _rec("999")])
    _wire(monkeypatch, store, known=["111"], argv=["--purge", "--all", "--yes"],
          deleter=lambda amem, cfg, *, case_id, **k: (
              purged.append(case_id) or SimpleNamespace(deleted=1, failed=0)))

    assert amem_purge.main() == 0
    assert "999" in purged


def test_a_failed_enumeration_says_so_rather_than_reporting_clean(monkeypatch, capsys):
    store = _Store([_rec("111")], fail=True)
    _wire(monkeypatch, store, known=["111"], argv=["--list"])

    amem_purge.main()
    out = capsys.readouterr().out
    assert "could not enumerate the store" in out
    assert "would NOT appear below" in out
    assert "ORPHAN" not in out  # nothing was learned, so nothing is claimed
