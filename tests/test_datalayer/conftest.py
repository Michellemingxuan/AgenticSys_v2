# tests/test_datalayer/conftest.py
"""Guard fixture: assert no test in this directory mutates real context/*.txt files.

The reconcile() reverse-sync path writes to context/*.txt files.  Tests that
call reconcile() MUST pass an isolated context_dir so they never touch the
real repo files.  This fixture catches any regression of that invariant.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def _guard_real_context_files():
    """Snapshot mtime of every real context/*.txt file; fail if any changed."""
    context_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "context")
    )
    if not os.path.isdir(context_dir):
        yield
        return

    # Snapshot: {filename: mtime}
    def _snapshot():
        snap = {}
        for fname in os.listdir(context_dir):
            if fname.endswith(".txt"):
                fpath = os.path.join(context_dir, fname)
                snap[fpath] = os.path.getmtime(fpath)
        return snap

    before = _snapshot()
    yield
    after = _snapshot()

    mutated = [
        p for p, mtime in after.items()
        if before.get(p) != mtime
    ]
    if mutated:
        names = [os.path.basename(p) for p in mutated]
        raise AssertionError(
            f"Test mutated real context file(s): {names}. "
            "Pass an isolated context_dir (e.g. str(tmp_path)) to reconcile() "
            "instead of relying on the production default."
        )
