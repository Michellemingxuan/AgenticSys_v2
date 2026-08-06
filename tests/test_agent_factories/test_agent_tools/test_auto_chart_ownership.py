"""A cross-domain peek must not draw the owning specialist's chart.

Charts render automatically from a specialist's trend outputs, so a specialist
that trends a column it doesn't own emits the owner's figure under its own
name. Measured live: `bureau` trended `credit_loss_prob_max` and
`tot_struct_risk_score_max` (CDSS / TSR — `modeling`'s metrics) beside its own
FICO series; cross-specialist dedup is first-writer-wins, so those plots were
attributed to `bureau` and `modeling` showed none of its own.
"""
from types import SimpleNamespace

from agent_factories.agent_tools.auto_chart import _drop_foreign_series
from agent_factories.agent_tools.series_extract import _ParsedSeries


class _Logger:
    def __init__(self):
        self.events = []

    def log(self, evt, payload):
        self.events.append((evt, payload))


def _series(column, table):
    return _ParsedSeries(lookup={"2025-01": 1.0, "2025-02": 2.0},
                         column_name=column, key_field="period",
                         table_name=table)


def _ctx(called):
    return SimpleNamespace(_domain_specialists_called=set(called))


def test_foreign_series_dropped_when_owner_also_ran():
    own = _series("FICO Score", "bureau_data")
    foreign = _series("tot_struct_risk_score_max", "model_scores")
    log = _Logger()

    kept = _drop_foreign_series([own, foreign], "bureau",
                                _ctx({"bureau", "modeling"}), log)

    assert [s.column_name for s in kept] == ["FICO Score"]
    assert any(e[0] == "auto_chart_foreign_series_dropped" for e in log.events)


def test_foreign_series_KEPT_when_owner_did_not_run():
    """If `modeling` isn't on the team, bureau's peek is the only source of
    that figure — dropping it would lose the chart entirely."""
    foreign = _series("tot_struct_risk_score_max", "model_scores")

    kept = _drop_foreign_series([foreign], "bureau", _ctx({"bureau"}), _Logger())

    assert [s.column_name for s in kept] == ["tot_struct_risk_score_max"]


def test_own_and_unknown_tables_always_kept():
    """Never drop what can't be positively attributed to someone else."""
    mine = _series("tot_struct_risk_score_max", "model_scores")
    untabled = _series("something", "")
    unknown = _series("x", "a_table_no_skill_declares")

    kept = _drop_foreign_series([mine, untabled, unknown], "modeling",
                                _ctx({"modeling", "bureau"}), _Logger())

    assert len(kept) == 3


# ── orphaned chart_pending placeholders ──────────────────────────────────────
#
# `chart_pending` fires per specialist DURING the turn; `chart` is emitted at
# END of turn, after cross-specialist dedup. A dropped chart leaves a
# placeholder waiting on an event that never arrives — a second,
# permanently-loading card beside the real one. The frontend cannot detect this
# on its own; nothing in the stream says the pending chart was superseded.


def test_record_chart_pending_accumulates_keys_on_ctx():
    from agent_factories.agent_tools.auto_chart import record_chart_pending

    ctx = SimpleNamespace()
    record_chart_pending(ctx, "bureau", "tsr_trend")
    record_chart_pending(ctx, "modeling", "tsr_trend")
    record_chart_pending(ctx, "bureau", "tsr_trend")  # idempotent

    assert ctx._charts_pending == {("bureau", "tsr_trend"),
                                   ("modeling", "tsr_trend")}


def test_record_chart_pending_never_raises_on_a_hostile_ctx():
    """Bookkeeping must never be what breaks a render."""
    from agent_factories.agent_tools.auto_chart import record_chart_pending

    class _NoSetattr:
        __slots__ = ()

    record_chart_pending(_NoSetattr(), "bureau", "t")  # must not raise
    record_chart_pending(None, "bureau", "t")
