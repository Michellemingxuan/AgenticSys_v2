"""`DataGateway.for_case` — a gateway bound to ONE case, sharing loaded data.

A single gateway carries one `_current_case`, so two concurrent turns on
different cases cannot share it: whichever calls `set_case` last silently
re-points every in-flight query on the other case. `for_case` forks the
scoping without forking the corpus.
"""
import pytest

from datalayer.gateway import DataGateway, LocalDataGateway


def _gw():
    return LocalDataGateway(case_data={
        "CASE-A": {"scores": [{"fico": 700}]},
        "CASE-B": {"scores": [{"fico": 620}]},
    })


def test_for_case_returns_a_gateway_bound_to_that_case():
    parent = _gw()
    a = parent.for_case("CASE-A")
    assert a.get_case_id() == "CASE-A"
    assert a.query("scores") == [{"fico": 700}]


def test_views_are_independent_and_do_not_touch_the_parent():
    parent = _gw()
    a, b = parent.for_case("CASE-A"), parent.for_case("CASE-B")

    assert a.query("scores") == [{"fico": 700}]
    assert b.query("scores") == [{"fico": 620}]
    # Re-scoping one view must not move the other, nor the parent.
    b.set_case("CASE-A")
    assert a.get_case_id() == "CASE-A"
    assert parent.get_case_id() is None


def test_the_table_corpus_is_shared_not_copied():
    """Per-session views must not multiply memory by the number of open cases —
    only `_current_case` is per-instance."""
    parent = _gw()
    a = parent.for_case("CASE-A")
    assert a._case_data is parent._case_data


def test_a_gateway_that_cannot_fork_says_so_rather_than_sharing_unsafely():
    class Frozen(DataGateway):
        def set_case(self, case_id): ...
        def get_case_id(self): return None
        def list_case_ids(self): return []
        def query(self, table, filters=None): return None
        def list_tables(self): return []

    with pytest.raises(NotImplementedError, match="must not be"):
        Frozen().for_case("CASE-A")
