"""Tests for tools/kb_tools.py — _active_kps and _format_kb_digest."""
from tools.kb_tools import _active_kps, _format_kb_digest


def test_active_kps_keeps_latest_per_topic():
    """Older KPs with the same topic are retained in the list (audit) but
    `_active_kps` returns only the most recent per topic."""
    kps = [
        {"topic": "monthly_spend_trend", "claim": "v1", "captured_at_turn": "t1"},
        {"topic": "top_merchants", "claim": "m1", "captured_at_turn": "t1"},
        {"topic": "monthly_spend_trend", "claim": "v2-revised", "captured_at_turn": "t2"},
    ]
    active = _active_kps(kps)
    by_topic = {k["topic"]: k["claim"] for k in active}
    # Latest one wins per topic, older still present in the source list
    # (audit log) but not returned by the active filter.
    assert by_topic == {"monthly_spend_trend": "v2-revised", "top_merchants": "m1"}
    assert len(kps) == 3  # source list untouched


def test_format_kb_digest_empty_when_no_kps():
    assert _format_kb_digest([]) == ""
    assert _format_kb_digest(None) == ""


def test_format_kb_digest_renders_active_set_only():
    """The digest lists active topic names and points to kb_lookup tools."""
    kps = [
        {"topic": "fico_trajectory", "claim": "FICO 720→680 over 6 months",
         "confidence": "high", "source_call": "summarize_trend('bureau','fico_score',...)"},
        {"topic": "fico_trajectory", "claim": "FICO 720→645 (revised)",
         "confidence": "medium"},
    ]
    digest = _format_kb_digest(kps)
    assert "fico_trajectory" in digest    # topic name shown
    assert "kb_lookup" in digest          # points to lookup tool
    assert "(1)" in digest                # deduped to 1 active topic


def test_format_kb_digest_shows_cross_specialist_topics():
    """When full_kb is provided, digest includes other specialists' topics."""
    own_kps = [{"topic": "gl_reductions", "claim": "Two GL cuts"}]
    full_kb = {
        "strategy": own_kps,
        "modeling": [{"topic": "tsr_trend", "claim": "TSR rose from 12 to 39"}],
        "bureau": [{"topic": "fico_trend", "claim": "FICO dropped to 680"}],
    }
    digest = _format_kb_digest(own_kps, full_kb=full_kb, self_name="strategy")
    assert "gl_reductions" in digest       # own topic shown
    assert "tsr_trend" in digest           # cross-specialist topic shown
    assert "fico_trend" in digest          # cross-specialist topic shown
    assert "modeling" in digest            # specialist name shown
    assert "strategy" not in digest.split("other specialists")[1]  # self excluded from "other"
