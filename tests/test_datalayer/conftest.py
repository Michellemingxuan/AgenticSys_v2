# tests/test_datalayer/conftest.py
"""Guard fixture: assert no test in this directory mutates real context/*.txt files.

The reconcile() reverse-sync path writes to context/*.txt files.  Tests that
call reconcile() MUST pass an isolated context_dir so they never touch the
real repo files.  This fixture catches any regression of that invariant.

Unlike the original mtime-only detection, this fixture is SELF-HEALING:
it snapshots the full file CONTENTS at setup, then at teardown restores any
mutated/deleted file and removes any file a test CREATED — leaving the real
context/ directory byte-for-byte identical to how it was found.  After
restoring it still FAILS the test loudly so the offending test is surfaced.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def _guard_real_context_files():
    """Snapshot + restore full content of every real context/*.txt file."""
    context_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "context")
    )
    if not os.path.isdir(context_dir):
        yield
        return

    # Snapshot: {absolute_path: bytes}
    def _snapshot():
        snap = {}
        for fname in os.listdir(context_dir):
            if fname.endswith(".txt"):
                fpath = os.path.join(context_dir, fname)
                try:
                    with open(fpath, "rb") as fh:
                        snap[fpath] = fh.read()
                except OSError:
                    pass
        return snap

    before = _snapshot()
    yield
    after = _snapshot()

    mutated = []

    # 1. Files that existed before: check if mutated or deleted.
    for fpath, original_bytes in before.items():
        current_bytes = after.get(fpath)
        if current_bytes is None:
            # File was DELETED — restore it.
            restore_label = "deleted, restored"
            try:
                with open(fpath, "wb") as fh:
                    fh.write(original_bytes)
            except OSError:
                restore_label = f"deleted, RESTORE FAILED: {os.path.basename(fpath)}"
            mutated.append(os.path.basename(fpath) + f" ({restore_label})")
        elif current_bytes != original_bytes:
            # File was MUTATED — restore original content.
            restore_label = "mutated, restored"
            try:
                with open(fpath, "wb") as fh:
                    fh.write(original_bytes)
            except OSError:
                restore_label = f"mutated, RESTORE FAILED: {os.path.basename(fpath)}"
            mutated.append(os.path.basename(fpath) + f" ({restore_label})")

    # 2. Files that were CREATED by the test (not in before snapshot) — remove.
    for fpath in after:
        if fpath not in before:
            try:
                os.remove(fpath)
            except OSError:
                pass
            mutated.append(os.path.basename(fpath) + " (created, removed)")

    if mutated:
        raise AssertionError(
            f"Test mutated real context file(s): {mutated}. "
            "Pass an isolated context_dir (e.g. str(tmp_path)) to reconcile() "
            "instead of relying on the production default. "
            "Restore attempted for each file — see labels above for any failures."
        )
