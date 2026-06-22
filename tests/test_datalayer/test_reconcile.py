# tests/test_datalayer/test_reconcile.py
from datalayer.gateway import LocalDataGateway
from datalayer.reconcile import check_consistency


def _gw(case_data):
    return LocalDataGateway(case_data=case_data)


def test_consistency_uniform_no_flags():
    gw = _gw({
        "c1": {"t": [{"a": 1, "b": 2}]},
        "c2": {"t": [{"a": 9, "b": 8}]},
    })
    res = check_consistency(gw)
    assert res.flags == []
    assert res.uniform_schema == {"t": {"a", "b"}}


def test_consistency_divergent_table_is_flagged_and_excluded():
    gw = _gw({
        "c1": {"t": [{"a": 1, "b": 2}]},
        "c2": {"t": [{"a": 9}]},          # missing column b
    })
    res = check_consistency(gw)
    assert "t" not in res.uniform_schema
    assert any("t" in f and "c2" in f for f in res.flags)
