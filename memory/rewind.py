"""Sync Amem delete-by-turn. Called from Flask rewind/cancel handlers. Deletes by
turn_id (a real scope field) — Amem cannot filter by session metadata.

SWALLOWED IS NOT SILENT (the same rule `memory/writer.py::_guard` states for the
write path). A purge must never break the rewind that asked for it, so every
exception here is caught — but a purge that quietly did nothing looked exactly
like one that worked. Clearing a case returned 204 with the UI wiped while the
memory survived, and neither the `rewind` log event nor anything else recorded a
single field about Amem.

A BARE COUNT CANNOT SAY WHY IT IS ZERO. `0` conflated three states:

  * nothing was there to delete           — success
  * `list_memories` raised (store down)   — total failure
  * every `delete_memory` failed          — total failure

so callers were structurally unable to tell them apart. These functions return a
`PurgeOutcome` instead: what was listed, what was confirmed deleted, what failed,
and the error if listing itself blew up. `.ok` is the question a caller actually
has — did the purge do what it claimed?
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import AmemConfig
from .scope import build_scope


def _log(logger, event: str, payload: dict) -> None:
    """Logging must never be what breaks the purge it is reporting on."""
    if logger is None:
        return
    try:
        logger.log(event, payload)
    except Exception:  # noqa: BLE001
        pass


@dataclass(frozen=True)
class PurgeOutcome:
    """Result of an Amem purge — the counts AND whether it actually worked."""

    listed: int = 0
    deleted: int = 0
    failed: int = 0
    #: Exception type from `list_memories`; set only when listing itself failed,
    #: which means the purge never got to look at anything.
    error: str | None = None
    #: True when the store is a `NullAmemManager` (Amem off / unreachable at
    #: boot). Distinct from an error: nothing was attempted, and that is
    #: correct — but it must not read as "purged successfully" either.
    skipped: bool = False

    @property
    def ok(self) -> bool:
        """Did every record we found actually get deleted?

        A skipped purge is `ok`: with Amem off there is nothing to purge. What
        is NOT ok is a store that answered and then lost records along the way.
        """
        return self.error is None and self.failed == 0

    def as_log_fields(self, prefix: str = "amem_purge") -> dict:
        """Flatten into a log payload so the caller's own event carries it."""
        return {
            f"{prefix}_ok": self.ok,
            f"{prefix}_listed": self.listed,
            f"{prefix}_deleted": self.deleted,
            f"{prefix}_failed": self.failed,
            f"{prefix}_error": self.error,
            f"{prefix}_skipped": self.skipped,
        }


@dataclass
class _Tally:
    """Mutable accumulator; `delete_turns` folds several scopes into one."""

    listed: int = 0
    deleted: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def finish(self, skipped: bool = False) -> PurgeOutcome:
        return PurgeOutcome(
            listed=self.listed, deleted=self.deleted, failed=self.failed,
            error=self.errors[0] if self.errors else None, skipped=skipped)


def _skip_if_disabled(amem, logger, *, op: str, case_id: str) -> bool:
    """True for `NullAmemManager` — Amem is off, so nothing can be purged.

    LOGGED, because this is the shape the prod failure actually takes.
    `build_amem_manager` falls back to `NullAmemManager` when the store is
    unreachable AT BOOT, so a server that came up without Qdrant purges
    nothing for the rest of its life while every clear still returns 204. The
    records the user is trying to remove — written by an earlier run that DID
    connect — are untouched and nothing said so.

    It is not an error (this process was never going to purge anything) but it
    is emphatically not "the case is now clear" either, so the caller gets
    `skipped=True` and the log gets an event to grep for.
    """
    if getattr(amem, "enabled", True):
        return False
    _log(logger, "amem_purge_skipped_store_disabled", {
        "op": op, "case_id": case_id,
        "detail": "Amem is a NullAmemManager (unreachable or disabled at boot); "
                  "no memory was purged for this case",
    })
    return True


def _purge_scope(amem, scope, tally: _Tally, logger, *, op: str) -> None:
    """List one scope and delete everything in it, folding results into `tally`.

    Two failure kinds, logged apart because they mean different things: listing
    failed (the store is unreachable — nothing was even attempted) versus a
    delete failed (the store answered, then did not remove a record it had just
    handed us).
    """
    try:
        records = amem.list_memories(scope=scope, include_working=True)
    except Exception as exc:  # noqa: BLE001 — a purge must not break the rewind
        tally.errors.append(type(exc).__name__)
        _log(logger, "amem_purge_list_failed", {
            "op": op, "case_id": scope.case_id, "turn_id": scope.turn_id,
            "error_type": type(exc).__name__, "error": str(exc)[:300],
        })
        return
    records = list(records or [])
    tally.listed += len(records)
    for rec in records:
        try:
            if amem.delete_memory(rec.id):
                tally.deleted += 1
            else:
                # The store answered without raising and still did not delete.
                tally.failed += 1
                _log(logger, "amem_purge_delete_refused", {
                    "op": op, "case_id": scope.case_id, "record_id": rec.id})
        except Exception as exc:  # noqa: BLE001
            tally.failed += 1
            _log(logger, "amem_purge_delete_failed", {
                "op": op, "case_id": scope.case_id, "record_id": rec.id,
                "error_type": type(exc).__name__, "error": str(exc)[:300],
            })


def delete_turns(amem, cfg: AmemConfig, *, case_id: str, turn_ids,
                 logger=None) -> PurgeOutcome:
    """Purge Amem memory for specific turns. Never raises."""
    if _skip_if_disabled(amem, logger, op="delete_turns", case_id=case_id):
        return _Tally().finish(skipped=True)
    tally = _Tally()
    for turn_id in turn_ids or []:
        _purge_scope(amem, build_scope(cfg, case_id, turn_id=turn_id), tally,
                     logger, op="delete_turns")
    return tally.finish()


def delete_case_memory(amem, cfg: AmemConfig, *, case_id: str,
                       logger=None) -> PurgeOutcome:
    """Purge ALL Amem memory for a case (working/conversation/case, every turn).
    Used by Clear History. Never raises."""
    if _skip_if_disabled(amem, logger, op="delete_case_memory", case_id=case_id):
        return _Tally().finish(skipped=True)
    tally = _Tally()
    _purge_scope(amem, build_scope(cfg, case_id),  # case-only: no turn/agent
                 tally, logger, op="delete_case_memory")
    return tally.finish()
