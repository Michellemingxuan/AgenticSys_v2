"""`sequence_join` — pair rows across two tables by TIME PROXIMITY.

`join_table` matches EQUAL keys, so it cannot express order + closeness in time
("a large spend right after a small payment"). Without a tool those questions
decompose into establish-thresholds / pull-A / pull-B / correlate-by-hand, which
measured live at 4-5 rounds and blew the specialist turn budget, returning
nothing at all.
"""
import json

import pytest

import tools.data_tools as dt


@pytest.fixture
def gw(monkeypatch):
    """Two tiny tables with hand-placed dates so every window is checkable."""
    payments = [
        {"payment_date": "2024-01-01", "payment_amount": "50"},    # small
        {"payment_date": "2024-03-01", "payment_amount": "9000"},  # large
        {"payment_date": "2024-06-10", "payment_amount": "20"},    # small, no follow
    ]
    spends = [
        {"spend_date": "2024-01-01", "amount": "8000", "merchant_name": "A"},  # same day
        {"spend_date": "2024-01-03", "amount": "7000", "merchant_name": "B"},  # +2d
        {"spend_date": "2024-01-09", "amount": "9500", "merchant_name": "C"},  # +8d
        {"spend_date": "2023-12-30", "amount": "8800", "merchant_name": "D"},  # -2d
        {"spend_date": "2024-03-02", "amount": "100", "merchant_name": "E"},   # tiny
    ]
    tables = {"payments": payments, "spends": spends}

    class _GW:
        def query(self, table, filters=None):
            rows = tables.get(table)
            return None if rows is None else [dict(r) for r in rows]

    monkeypatch.setattr(dt, "_gateway", _GW())
    monkeypatch.setattr(dt, "_resolve_real_table", lambda t: t)
    return tables


def _run(**kw):
    kw.setdefault("anchor_table", "payments")
    kw.setdefault("follow_table", "spends")
    kw.setdefault("anchor_time_column", "payment_date")
    kw.setdefault("follow_time_column", "spend_date")
    return json.loads(dt._sequence_join_impl(**kw))


def test_finds_large_spend_within_window_after_small_payment(gw):
    r = _run(within_days=3, direction="after",
             anchor_filters='[{"column":"payment_amount","op":"lt","value":"1000"}]',
             follow_filters='[{"column":"amount","op":"gt","value":"5000"}]')

    # Same-day (gap 0) and +2d match; +8d is outside the window; the -2d spend
    # is BEFORE, so `direction="after"` must exclude it.
    assert r["pairs_found"] == 2
    assert sorted(p["gap_days"] for p in r["pairs"]) == [0, 2]
    assert {p["follow"]["merchant_name"] for p in r["pairs"]} == {"A", "B"}
    assert r["anchors_with_match"] == 1


def test_direction_before_and_either(gw):
    small = '[{"column":"payment_amount","op":"lt","value":"1000"}]'
    big = '[{"column":"amount","op":"gt","value":"5000"}]'

    before = _run(within_days=3, direction="before",
                  anchor_filters=small, follow_filters=big)
    assert {p["follow"]["merchant_name"] for p in before["pairs"]} == {"A", "D"}

    either = _run(within_days=3, direction="either",
                  anchor_filters=small, follow_filters=big)
    assert {p["follow"]["merchant_name"] for p in either["pairs"]} == {"A", "B", "D"}


def test_within_days_zero_is_same_calendar_day(gw):
    r = _run(within_days=0, direction="after",
             anchor_filters='[{"column":"payment_amount","op":"lt","value":"1000"}]',
             follow_filters='[{"column":"amount","op":"gt","value":"5000"}]')
    assert [p["follow"]["merchant_name"] for p in r["pairs"]] == ["A"]
    # The grain limit travels with the answer so nobody implies intraday order.
    assert r["time_grain"] == "day"


def test_zero_pairs_reports_the_search_space_as_a_measured_negative(gw):
    """A null result is only a finding if you can say how hard you looked."""
    r = _run(within_days=1, direction="after",
             anchor_filters='[{"column":"payment_amount","op":"gt","value":"5000"}]',
             follow_filters='[{"column":"amount","op":"gt","value":"5000"}]')

    assert r["pairs_found"] == 0
    assert r["not_tested"] is False
    assert r["anchor"]["rows_matching"] == 1      # the 9000 payment
    assert r["follow"]["rows_matching"] == 4
    assert "measured negative" in r["no_match_summary"]


def test_empty_side_is_NOT_TESTED_not_a_negative_finding(gw):
    """Zero anchors means the FILTER selected nothing — the question was never
    tested. Reporting that as "no such pattern" would be a false negative."""
    r = _run(within_days=3, direction="after",
             anchor_filters='[{"column":"payment_amount","op":"lt","value":"1"}]',
             follow_filters='[{"column":"amount","op":"gt","value":"5000"}]')

    assert r["pairs_found"] == 0
    assert r["not_tested"] is True
    assert "NOT TESTED" in r["no_match_summary"]
    assert "not evidence that the pattern is absent" in r["no_match_summary"].lower()


def test_unparseable_dates_are_counted_not_silently_dropped(gw):
    gw["spends"].append({"spend_date": "not-a-date", "amount": "9999",
                         "merchant_name": "X"})
    r = _run(within_days=3, direction="after",
             follow_filters='[{"column":"amount","op":"gt","value":"5000"}]')
    assert r["unparseable_dates"]["follow_rows"] == 1


def test_columns_projection_and_limit(gw):
    r = _run(within_days=30, direction="either",
             anchor_columns="payment_date",
             follow_columns="merchant_name",
             limit=1)
    assert r["rows_returned"] == 1
    assert set(r["pairs"][0]["anchor"]) == {"payment_date"}
    assert set(r["pairs"][0]["follow"]) == {"merchant_name"}
    assert r["truncated"] is True
    assert r["pairs_found"] > 1
