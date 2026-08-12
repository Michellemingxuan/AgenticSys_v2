"""Tests for tools.distiller_pass: _distill_and_persist standalone behavior.

Moved from tests/test_agent_factories/test_redacting_tool.py as part of the
redacting_tool decomposition (Task 2 of 5).
"""
import asyncio

from agent_factories.agent_tools.distiller_pass import _distill_and_persist


def test_slug_topic_is_readable_and_deterministic():
    """Narrow-output KPs get a readable topic from the sub-question, not an
    opaque hash — same question → same slug (KB dedup), empty → hash fallback."""
    from agent_factories.agent_tools.distiller_pass import _slug_topic
    t = _slug_topic("what is the total spend and payment status", "spend_payments")
    assert t == "total_spend_payment_status"
    # deterministic
    assert _slug_topic("what is the total spend and payment status",
                       "spend_payments") == t
    # no opaque {name}_q_<hash> for a normal question
    assert "_q_" not in t
    # empty sub-question → name-scoped hash fallback (never a bare/empty topic)
    fb = _slug_topic("", "spend_payments")
    assert fb.startswith("spend_payments_q_") and len(fb) > len("spend_payments_q_")


def test_distill_and_persist_noop_when_distiller_unwired():
    """Tests / legacy paths without _distiller or _specialist_kb must behave
    like the legacy single-turn flow — no errors, no KB updates."""
    from types import SimpleNamespace

    ctx_no_distiller = SimpleNamespace(
        logger=None, _specialist_kb={}, _distiller=None, _turn_id=None,
    )
    n = asyncio.run(
        _distill_and_persist(ctx_no_distiller, "x", "q", "out")
    )
    assert n == 0
    assert ctx_no_distiller._specialist_kb == {}


def test_distill_and_persist_skips_report_agent():
    """report_agent returns ReportDraft (narrative), not SpecialistOutput —
    running the distiller on it costs ~20s for trivial KPs. The wrapper
    must short-circuit so neither the distiller LLM call nor the KB write
    happens for report_agent. The distiller mock is asserted untouched."""
    from types import SimpleNamespace

    distiller_calls: list = []

    class _MockDistiller:
        def __call__(self, *args, **kwargs):
            distiller_calls.append((args, kwargs))
            raise AssertionError("distiller should not have been invoked for report_agent")

    logged_events: list = []

    class _MockLogger:
        def log(self, event, payload):
            logged_events.append((event, payload))

    ctx = SimpleNamespace(
        logger=_MockLogger(),
        _specialist_kb={},
        _distiller=_MockDistiller(),
        _turn_id="t-1",
    )
    n = asyncio.run(
        _distill_and_persist(ctx, "report_agent", "q", "out")
    )
    assert n == 0
    assert distiller_calls == []
    assert ctx._specialist_kb == {}
    # The skip is observable in the JSONL so perf regressions are auditable.
    assert any(e == "distiller_skipped" and p.get("specialist") == "report_agent"
               for e, p in logged_events)


# ── a truncated distiller response must cost one KP, not all of them ────────


def test_salvage_recovers_the_complete_kps_before_the_cut():
    """The dominant distiller failure (26 of 38 logged): the skill asks for
    every row of every series AND a key per period even where the value is
    null, so a few KPs run past the output budget and the JSON stops
    mid-array. `Invalid JSON when parsing {…` used to drop the whole turn's
    knowledge; the objects that closed are perfectly good."""
    from agent_factories.agent_tools.distiller_pass import _salvage_truncated_kps

    msg = ('Invalid JSON when parsing {"knowledge_points":['
           '{"topic":"fico_trajectory","claim":"FICO stable 745-821.",'
           '"numbers":[{"period":"2023-07","value":null}]},'
           '{"topic":"ln_score","claim":"LN score fell to 612.","numbers":[]},'
           '{"topic":"delinq","claim":"Cut off mid')
    kps = _salvage_truncated_kps(Exception(msg))

    assert [k["topic"] for k in kps] == ["fico_trajectory", "ln_score"]
    assert kps[0]["numbers"][0]["period"] == "2023-07"


def test_salvage_ignores_a_kp_missing_its_claim():
    """A KP can close syntactically and still be unusable — the KB digest
    renders a claimless point as a blank line."""
    from agent_factories.agent_tools.distiller_pass import _salvage_truncated_kps

    msg = ('Invalid JSON when parsing {"knowledge_points":['
           '{"topic":"only_a_topic"},'
           '{"topic":"good","claim":"Real claim."},')
    assert [k["topic"] for k in _salvage_truncated_kps(Exception(msg))] == ["good"]


def test_salvage_is_not_confused_by_braces_inside_strings():
    """A claim quoting JSON-ish text must not close an object early."""
    from agent_factories.agent_tools.distiller_pass import _salvage_truncated_kps

    msg = ('Invalid JSON when parsing {"knowledge_points":['
           '{"topic":"t1","claim":"Payload was {\\"a\\": 1} and it failed."},'
           '{"topic":"t2","claim":"Second.","numbers":[]},'
           '{"topic":"t3","claim":"trunc')
    assert [k["topic"] for k in _salvage_truncated_kps(Exception(msg))] == ["t1", "t2"]


def test_salvage_returns_empty_when_there_is_nothing_to_recover():
    """Timeouts, rate limits and a cut that lands before the first complete KP
    all fall through to the existing failure path."""
    from agent_factories.agent_tools.distiller_pass import _salvage_truncated_kps

    assert _salvage_truncated_kps(TimeoutError("timed out")) == []
    assert _salvage_truncated_kps(Exception("Invalid JSON when parsing {")) == []
    assert _salvage_truncated_kps(Exception(
        'Invalid JSON when parsing {"knowledge_points":[{"topic":"cut')) == []
