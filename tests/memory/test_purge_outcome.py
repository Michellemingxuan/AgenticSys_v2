"""A purge must report whether it actually happened.

`delete_turns` / `delete_case_memory` swallow every exception so a failing
store cannot break the rewind that asked for it. They used to return a bare
`int`, which made `0` mean three different things — nothing to delete, the
store was down, or every delete failed — so no caller could tell a successful
clear from one that silently did nothing. These tests pin each state apart.
"""
from memory.config import AmemConfig
from memory.null_manager import NullAmemManager
from memory.rewind import PurgeOutcome, delete_case_memory, delete_turns
from tests.memory._fake_amem import FakeAmem, FakeRecord

CFG = AmemConfig(enabled=True, store_url="x", collection_name="c", vector_size=3072,
                 read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
                 org_id="amx", user_id="amx_reviewer")


class _Recorder:
    """Captures logger.log(event, payload) calls."""

    def __init__(self):
        self.events = []

    def log(self, event, payload):
        self.events.append((event, payload))

    def names(self):
        return [e for e, _ in self.events]


# ── the three states a bare 0 used to conflate ──────────────────────────────


def test_empty_case_is_a_successful_purge():
    """Nothing to delete is success — the case really is clear afterwards."""
    fake = FakeAmem()
    fake.listed = []
    r = delete_case_memory(fake, CFG, case_id="c1")
    assert (r.listed, r.deleted, r.failed) == (0, 0, 0)
    assert r.ok is True and r.error is None and r.skipped is False


def test_unreachable_store_is_not_a_successful_purge():
    """The regression this exists for: the store raised, nothing was even
    attempted, and the old return value (0) was identical to an empty case."""
    class Down(FakeAmem):
        def list_memories(self, **k):
            raise ConnectionError("refused")

    r = delete_case_memory(Down(), CFG, case_id="c1")
    assert r.deleted == 0
    assert r.ok is False
    assert r.error == "ConnectionError"


def test_delete_failures_are_not_a_successful_purge():
    """The store answered, handed us records, then failed to remove them.
    Counting only successes would report 0 deleted and call it fine."""
    class HalfBroken(FakeAmem):
        def delete_memory(self, rec_id):
            if rec_id == "b":
                raise RuntimeError("write denied")
            return True

    fake = HalfBroken()
    fake.listed = [FakeRecord(id="a", content="x"), FakeRecord(id="b", content="y")]
    r = delete_case_memory(fake, CFG, case_id="c1")
    assert (r.listed, r.deleted, r.failed) == (2, 1, 1)
    assert r.ok is False
    assert r.error is None          # listing worked; the deletes did not


def test_a_refused_delete_counts_as_a_failure():
    """`delete_memory` returning falsey is a refusal, not a no-op — the record
    is still there and the caller must not be told the case is clear."""
    class Refuses(FakeAmem):
        def delete_memory(self, rec_id):
            return False

    fake = Refuses()
    fake.listed = [FakeRecord(id="a", content="x")]
    r = delete_case_memory(fake, CFG, case_id="c1")
    assert (r.deleted, r.failed) == (0, 1)
    assert r.ok is False


def test_disabled_store_is_skipped_not_failed():
    """Amem off is neither success-with-work nor an error: nothing was
    attempted and that is correct. It must not read as a purge that ran."""
    r = delete_case_memory(NullAmemManager(), CFG, case_id="c1")
    assert r.skipped is True
    assert r.ok is True             # nothing to purge, so nothing went wrong
    assert (r.listed, r.deleted, r.failed) == (0, 0, 0)


# ── logging ─────────────────────────────────────────────────────────────────


def test_a_failed_listing_is_logged_distinctly_from_a_failed_delete():
    """Different fixes: listing failed means the store is unreachable; a delete
    failed means it answered and then lost a record."""
    class Down(FakeAmem):
        def list_memories(self, **k):
            raise ConnectionError("refused")

    log = _Recorder()
    delete_case_memory(Down(), CFG, case_id="c1", logger=log)
    assert "amem_purge_list_failed" in log.names()

    class DeleteBoom(FakeAmem):
        def delete_memory(self, rec_id):
            raise RuntimeError("nope")

    fake = DeleteBoom()
    fake.listed = [FakeRecord(id="a", content="x")]
    log2 = _Recorder()
    delete_case_memory(fake, CFG, case_id="c1", logger=log2)
    assert "amem_purge_delete_failed" in log2.names()
    assert "amem_purge_list_failed" not in log2.names()


def test_a_successful_purge_logs_nothing():
    """Only failures are events. A clean purge is reported through the caller's
    own `rewind` event, not by adding noise here."""
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="a", content="x")]
    log = _Recorder()
    delete_case_memory(fake, CFG, case_id="c1", logger=log)
    assert log.names() == []


def test_logging_never_breaks_the_purge():
    """A broken logger must not turn a working purge into a failed one."""
    class BadLogger:
        def log(self, *a, **k):
            raise RuntimeError("logger down")

    class Down(FakeAmem):
        def list_memories(self, **k):
            raise ConnectionError("refused")

    r = delete_case_memory(Down(), CFG, case_id="c1", logger=BadLogger())
    assert r.ok is False            # returned normally rather than raising


def test_no_logger_is_accepted():
    """Callers that don't log still work — `logger` is optional."""
    fake = FakeAmem()
    fake.listed = [FakeRecord(id="a", content="x")]
    assert delete_case_memory(fake, CFG, case_id="c1").deleted == 1


# ── multi-turn folding + log payload ────────────────────────────────────────


def test_delete_turns_folds_every_scope_into_one_outcome():
    """One bad turn among several must not be averaged away into a clean
    result — the whole purge is reported as incomplete."""
    class OneTurnDown(FakeAmem):
        def list_memories(self, **k):
            if k["scope"].turn_id == "t2":
                raise ConnectionError("refused")
            return [FakeRecord(id="a", content="x")]

    r = delete_turns(OneTurnDown(), CFG, case_id="c1", turn_ids=["t1", "t2", "t3"])
    assert r.deleted == 2           # t1 and t3 succeeded
    assert r.ok is False            # t2 did not
    assert r.error == "ConnectionError"


def test_as_log_fields_carries_the_whole_outcome():
    fields = PurgeOutcome(listed=7, deleted=5, failed=2,
                          error=None).as_log_fields()
    assert fields == {
        "amem_purge_ok": False,
        "amem_purge_listed": 7,
        "amem_purge_deleted": 5,
        "amem_purge_failed": 2,
        "amem_purge_error": None,
        "amem_purge_skipped": False,
    }


def test_a_disabled_store_logs_that_nothing_was_purged():
    """The shape the prod failure actually takes. `build_amem_manager` falls
    back to NullAmemManager when the store is unreachable AT BOOT, so a server
    that came up without Qdrant purges nothing for its whole life while every
    clear still returns 204 — and the records the user is trying to remove,
    written by an earlier run that DID connect, sit there untouched.

    Confirmed live: pointing AMEM_STORE_URL at a dead port yielded
    skipped=True with no event at all before this.
    """
    log = _Recorder()
    r = delete_case_memory(NullAmemManager(), CFG, case_id="c1", logger=log)
    assert r.skipped is True
    assert "amem_purge_skipped_store_disabled" in log.names()
    payload = dict(log.events)["amem_purge_skipped_store_disabled"]
    assert payload["case_id"] == "c1"
    assert payload["op"] == "delete_case_memory"


def test_disabled_store_skips_the_turn_purge_too():
    log = _Recorder()
    r = delete_turns(NullAmemManager(), CFG, case_id="c1", turn_ids=["t1"], logger=log)
    assert r.skipped is True
    assert dict(log.events)["amem_purge_skipped_store_disabled"]["op"] == "delete_turns"
