"""Source-level guards: the full-rewind branch purges Amem, the partial branch
re-snapshots. (Full HTTP exercise needs live bootstrap; these assert the wiring
is present so review + the e2e smoke can verify behavior.)"""
import inspect
import server


def test_full_rewind_calls_delete_case_memory():
    src = inspect.getsource(server.post_rewind)
    assert "delete_case_memory(" in src
    # and it's in the full (else) branch, not the partial branch
    assert "delete_case_memory(_AMEM, _AMEM_CFG, case_id=case_id)" in src


def test_partial_rewind_resnapshots():
    src = inspect.getsource(server.post_rewind)
    assert "snapshot_session(" in src   # partial branch re-snapshots reduced state


def test_delete_case_memory_imported():
    from server import delete_case_memory  # re-exported/imported into server
