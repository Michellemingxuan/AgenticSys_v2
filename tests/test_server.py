"""Tests for server.py helpers that are testable without booting the Flask app.

Importing server.py runs heavy bootstrap (data source resolution, catalog,
firewall stack), so we only target pure helpers — e.g., the KB-warmth hint
and bounded session-memory helpers.
"""

import os
import sys
from types import SimpleNamespace

# Force a deterministic data source so the bootstrap doesn't try to reach
# external services when this test module is imported.
os.environ.setdefault("DATA_SOURCE", "generator")
os.environ.setdefault("MODEL", "gpt-4.1")

# Ensure the repo root is on sys.path so `import server` works regardless
# of where pytest is invoked from.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import server  # noqa: E402

from runner.turn import cache  # noqa: E402
from runner.turn import conductor  # noqa: E402
from runner.turn import finalize  # noqa: E402
from runner.turn.input_assembly import _format_kb_warmth_hint  # noqa: E402


# ── KB-warmth hint ──────────────────────────────────────────────────────────


def test_format_kb_warmth_hint_lists_warm_specialists():
    kb = {
        "spend_payments": [{"topic": "a", "claim": "x"},
                           {"topic": "b", "claim": "y"},
                           {"topic": "c", "claim": "z"}],
        "modeling": [{"topic": "d", "claim": "w"},
                     {"topic": "e", "claim": "v"}],
        "bureau": [],  # empty → must NOT appear in the hint
    }
    hint = _format_kb_warmth_hint(kb)
    assert hint.startswith("[KB-warmth —")
    assert "spend_payments (3 KPs)" in hint
    assert "modeling (2 KPs)" in hint
    assert "bureau" not in hint
    # Specialists are listed in alphabetical order by name.
    assert hint.index("modeling") < hint.index("spend_payments")
    # Each warm specialist also lists its per-topic claims.
    assert "- a: x" in hint
    assert "- d: w" in hint


def test_format_kb_warmth_hint_preserves_full_claim_and_marks_truncation():
    """Claims up to the (raised) limit appear in full — a two-metric claim is
    no longer cut mid-fact at 120 chars; only genuinely over-long claims are
    truncated, and then with a visible '…'."""
    from runner.turn.input_assembly import _KB_WARMTH_CLAIM_CHARS
    mid = "M" * 200                               # under the limit -> shown whole
    long = "L" * (_KB_WARMTH_CLAIM_CHARS + 50)    # over the limit -> clipped + "…"
    kb = {"modeling": [{"topic": "mid", "claim": mid},
                       {"topic": "long", "claim": long}]}
    hint = _format_kb_warmth_hint(kb)
    assert mid in hint                                       # full 200 chars (old 120 would cut)
    assert ("L" * _KB_WARMTH_CLAIM_CHARS) + "…" in hint      # truncated at limit + ellipsis
    assert long not in hint                                  # not shown in full


def test_format_kb_warmth_hint_empty_when_no_warm_specialists():
    """Empty KB or all-empty values → empty hint, so the orchestrator's
    prompt isn't cluttered on the first turn or after /rewind."""
    assert _format_kb_warmth_hint({}) == ""
    assert _format_kb_warmth_hint({"x": [], "y": []}) == ""
    assert _format_kb_warmth_hint(None) == ""


# ── Bounded session memory ──────────────────────────────────────────────────


def test_store_cached_qa_evicts_oldest_entry(monkeypatch):
    monkeypatch.setattr(cache, "_QA_CACHE_MAX_ENTRIES", 2)
    sess = SimpleNamespace(qa_cache={}, _qa_turn_seq=0)

    cache._store_cached_qa(sess, "q1", {"answer": "a1"})
    cache._store_cached_qa(sess, "q2", {"answer": "a2"})
    # Touch q1 so q2 becomes the least-recently used entry.
    assert cache._get_cached_qa(sess, "q1")["answer"] == "a1"
    evicted = cache._store_cached_qa(sess, "q3", {"answer": "a3"})

    assert evicted == 1
    assert list(sess.qa_cache) == ["q1", "q3"]
    assert "q2" not in sess.qa_cache


def test_case_session_emit_drops_oldest_when_subscriber_queue_full():
    import queue

    sess = server.CaseSession(
        case_id="C",
        gateway=None,
        catalog=None,
        clients=None,
        pillar_yaml={},
        chat_agent=None,
        logger=None,
    )
    sub_q = queue.Queue(maxsize=1)
    sub_q.put(("old", {"n": 1}))
    sess.subscribers.append(sub_q)

    sess.emit("new", {"n": 2})

    assert sub_q.qsize() == 1
    assert sub_q.get_nowait() == ("new", {"n": 2})


def test_case_session_exposes_inflight_cancellation_fields():
    """`post_rewind` reaches into the session to abort the in-flight
    turn. Two pieces must exist on every CaseSession or rewind silently
    falls back to lock-wait-and-pray:

      - `cancel_in_flight: threading.Event` — cooperative signal that
        `_run_turn_streamed` checkpoints honor between awaits.
      - `current_inflight` — `(loop, task)` tuple set by the runner so
        `post_rewind` can `loop.call_soon_threadsafe(task.cancel)` and
        interrupt the active LLM await directly. Default `None` when
        no turn is in flight.

    Regression test: ensure these aren't removed when the dataclass
    is reorganized."""
    import threading

    sess = server.CaseSession(
        case_id="C",
        gateway=None,
        catalog=None,
        clients=None,
        pillar_yaml={},
        chat_agent=None,
        logger=None,
    )
    assert isinstance(sess.cancel_in_flight, threading.Event)
    assert sess.cancel_in_flight.is_set() is False
    assert sess.current_inflight is None


def test_turn_aborted_class_is_a_distinct_exception():
    """`_TurnAborted` is the signal raised from cooperative checkpoints
    inside `_run_turn_streamed`. It must be a subclass of Exception
    (so the runner's catch reaches it) but distinct from
    `asyncio.CancelledError` (which is also caught, but logged with
    a different reason) and from `_TurnAborted` getting swept up by
    a bare `except Exception` somewhere upstream."""
    import asyncio
    assert issubclass(conductor._TurnAborted, Exception)
    assert not issubclass(conductor._TurnAborted, asyncio.CancelledError)


# ── Prewarm path ────────────────────────────────────────────────────────────


def _fake_sess_for_scope(case_id):
    import types
    from datalayer.gateway import LocalDataGateway
    gw = LocalDataGateway(case_data={case_id: {"t": [{"c": case_id}]}})
    gw.set_case(case_id)
    return types.SimpleNamespace(
        case_id=case_id, gateway=gw, catalog=object(),
        logger=types.SimpleNamespace(log=lambda *a, **k: None),
    )


def test_case_scope_binds_this_session_gateway_for_the_turn():
    """Replaces the old `_rescope_to_case`, which re-pointed process globals.
    Reads inside the block must resolve to THIS session's gateway."""
    from tools import data_tools

    sess = _fake_sess_for_scope("CASE-A")
    with server._case_scope(sess):
        assert data_tools._gw() is sess.gateway
        assert data_tools._gw().get_case_id() == "CASE-A"


def test_case_scope_restores_on_exit_so_a_reused_thread_cannot_leak():
    from tools import data_tools

    before = data_tools._gw()
    sess = _fake_sess_for_scope("CASE-A")
    with server._case_scope(sess):
        pass
    assert data_tools._gw() is before


def test_concurrent_turns_on_different_cases_do_not_re_point_each_other():
    """The regression this whole change exists for. Two turns on different
    cases, running at once, each in its own thread — neither may observe the
    other's gateway. Before, both read whichever case bound the global last."""
    import threading

    from tools import data_tools

    seen: dict[str, list] = {}
    step = threading.Barrier(2, timeout=5)

    def run_turn(case_id):
        sess = _fake_sess_for_scope(case_id)
        with server._case_scope(sess):
            step.wait()          # both inside their scope simultaneously
            observed = [data_tools._gw().get_case_id(),
                        data_tools._gw().query("t")]
            step.wait()          # hold both scopes open while the other reads
        seen[case_id] = observed

    threads = [threading.Thread(target=run_turn, args=(c,))
               for c in ("CASE-A", "CASE-B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert seen["CASE-A"] == ["CASE-A", [{"c": "CASE-A"}]]
    assert seen["CASE-B"] == ["CASE-B", [{"c": "CASE-B"}]]


def test_prewarm_respects_env_skip(monkeypatch):
    """`LLM_PREWARM=0` makes `_prewarm_clients()` a no-op. Verifies the
    skip path emits the skipped event AND does NOT touch the LLM
    client (which would otherwise make a real network call)."""
    monkeypatch.setenv("LLM_PREWARM", "0")
    # Sentinel that would explode if the prewarm tries to use it.
    class _BoomClient:
        @property
        def chat(self):
            raise RuntimeError("prewarm should NOT touch the client when disabled")
    monkeypatch.setattr(server._CLIENTS, "firewalled_client", _BoomClient(), raising=False)
    # Should return cleanly without raising.
    server._prewarm_clients()


def test_prewarm_swallows_failures(monkeypatch):
    """Prewarm is best-effort — a failure must NOT prevent the server
    from finishing startup. We force the client to raise and verify
    `_prewarm_clients` returns normally and logs the failure event."""
    monkeypatch.setenv("LLM_PREWARM", "1")

    logged: list = []

    class _BoomLogger:
        def log(self, ev, payload):
            logged.append((ev, payload))

    class _BoomCompletions:
        async def create(self, **kw):  # noqa: ARG002
            raise RuntimeError("simulated cold-start failure")
    class _BoomChat:
        completions = _BoomCompletions()
    class _BoomClient:
        chat = _BoomChat()

    monkeypatch.setattr(server, "_BOOT_LOGGER", _BoomLogger(), raising=False)
    monkeypatch.setattr(server._CLIENTS, "firewalled_client", _BoomClient(), raising=False)

    # Must NOT raise — failure path is the whole point of the
    # broad except.
    server._prewarm_clients()
    assert any(ev == "llm_prewarm_failed" for ev, _ in logged), (
        f"prewarm failure must log llm_prewarm_failed; got events: {logged}"
    )


# ── Phase 2 — turn-chart collection + answer-text appending ──────────────────


def test_collect_turn_charts_filters_by_turn_and_image_path():
    """Only KPs with `captured_at_turn == turn_id` AND a non-empty
    `image_path` are surfaced as charts to embed."""
    kb = {
        "spend_payments": [
            # In this turn, has chart — should appear
            {"topic": "trend", "captured_at_turn": "t-now",
             "image_path": "/abs/foo/charts/t-now-trend.png"},
            # In this turn, no chart — should NOT appear
            {"topic": "no_chart", "captured_at_turn": "t-now"},
            # Earlier turn, has chart — should NOT appear (only this turn's)
            {"topic": "old", "captured_at_turn": "t-prev",
             "image_path": "/abs/foo/charts/t-prev-old.png"},
        ],
        "modeling": [
            {"topic": "delinq", "captured_at_turn": "t-now",
             "image_path": "/abs/foo/charts/t-now-delinq.png"},
        ],
    }
    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-1")
    topics = {c["topic"] for c in charts}
    assert topics == {"trend", "delinq"}
    # URL points at the Flask route, not the on-disk path.
    for c in charts:
        assert c["url"].startswith("/api/cases/CASE-1/charts/")
        assert c["url"].endswith(".png")


def test_collect_turn_charts_dedupes_same_topic_per_specialist():
    """When both `make_chart` and the auto-distiller produce a chart for
    the same (specialist, topic) in one turn, only the latest entry
    surfaces — so the reviewer sees one chart per topic, not two."""
    kb = {
        "spend_payments": [
            # Earlier (e.g. make_chart explicit): same topic, same turn
            {"topic": "monthly_trend", "captured_at_turn": "t-now",
             "image_path": "/abs/case/charts/t-now-monthly_trend.png"},
            # Later (e.g. distiller): same topic, same turn — should win
            {"topic": "monthly_trend", "captured_at_turn": "t-now",
             "image_path": "/abs/case/charts/t-now-monthly_trend-v2.png"},
            # Different topic, same turn — independent, both included
            {"topic": "merchants", "captured_at_turn": "t-now",
             "image_path": "/abs/case/charts/t-now-merchants.png"},
        ],
    }
    charts = finalize._collect_turn_charts(kb, "t-now", "C1")
    topics = sorted(c["topic"] for c in charts)
    assert topics == ["merchants", "monthly_trend"]
    # The dedup target carries the LATEST image_path (the v2 one).
    monthly = next(c for c in charts if c["topic"] == "monthly_trend")
    assert monthly["url"].endswith("t-now-monthly_trend-v2.png")


def test_collect_turn_charts_does_not_dedupe_when_no_fingerprint():
    """Two specialists with the same topic but NO fingerprintable content
    (no viz/numbers) both appear — same topic name ≠ same figure, and there is
    no data to compare, so nothing is deduped."""
    kb = {
        "spend_payments": [
            {"topic": "x", "captured_at_turn": "t",
             "image_path": "/abs/case/charts/t-x-sp.png"},
        ],
        "modeling": [
            {"topic": "x", "captured_at_turn": "t",
             "image_path": "/abs/case/charts/t-x-mod.png"},
        ],
    }
    charts = finalize._collect_turn_charts(kb, "t", "C")
    specialists = sorted(c["specialist"] for c in charts)
    assert specialists == ["modeling", "spend_payments"]


def test_collect_turn_charts_dedupes_same_figure_across_specialists():
    """Two specialists plotting the SAME data (e.g. modeling AND spend_payments
    both charting the monthly spend trend) surface as ONE chart — deduped by
    content fingerprint, not topic name. Different data is NOT deduped."""
    numbers = [{"period": "2024-11", "value": 300},
               {"period": "2024-12", "value": 500}]
    kb = {
        # dispatch order: spend_payments first → it wins the dedup.
        "spend_payments": [_kp("monthly_spend_trend", "trend", ["value"], numbers)],
        "modeling": [_kp("spend_amount_trend", "trend", ["value"], list(numbers))],
    }
    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-D")
    assert len(charts) == 1
    assert charts[0]["specialist"] == "spend_payments"

    # Different data → two distinct figures, both kept.
    kb["modeling"] = [_kp("tsr_trend", "trend", ["value"],
                          [{"period": "2024-11", "value": 10},
                           {"period": "2024-12", "value": 20}])]
    assert len(finalize._collect_turn_charts(kb, "t-now", "CASE-D")) == 2


def test_collect_turn_charts_never_dedupes_flat_series():
    """False-positive guard: two DIFFERENT all-constant (e.g. all-zero) metrics
    have identical weak signatures — but a flat series is too weak to tell them
    apart, so they are NEVER deduped. Both survive."""
    flat_a = [{"period": "2024-11", "value": 0}, {"period": "2024-12", "value": 0}]
    flat_b = [{"period": "2024-11", "value": 0}, {"period": "2024-12", "value": 0}]
    kb = {
        "modeling": [_kp("dead_feature_a", "trend", ["value"], flat_a)],
        "spend_payments": [_kp("dead_feature_b", "trend", ["value"], flat_b)],
    }
    assert len(finalize._collect_turn_charts(kb, "t-now", "C")) == 2


def test_collect_turn_charts_never_dedupes_when_x_field_absent():
    """False-positive guard: when the x-axis column isn't present in the rows,
    the signature would collapse to a y-only set (different windows collide) —
    so such charts are never deduped."""
    # x_field is "period" (from _kp) but the rows key it as "month".
    a = [{"month": "2024-01", "value": 1}, {"month": "2024-02", "value": 2}]
    b = [{"month": "2024-07", "value": 1}, {"month": "2024-08", "value": 2}]
    kb = {
        "modeling": [_kp("m_a", "trend", ["value"], a)],
        "spend_payments": [_kp("m_b", "trend", ["value"], b)],
    }
    assert len(finalize._collect_turn_charts(kb, "t-now", "C")) == 2


def test_collect_turn_charts_surfaces_table_kps_without_image():
    """KPs with `viz.kind == "table"` carry row data but no image_path.
    They MUST surface as chart events so the frontend can render the
    rows as an HTML table card in the Plots panel — otherwise small
    datasets the specialist deliberately routed through `kind='table'`
    would silently disappear."""
    kb = {
        "spend_payments": [
            # Plot-style KP — has image_path, surfaces with URL.
            {"topic": "trend", "captured_at_turn": "t-now",
             "viz": {"kind": "trend"},
             "image_path": "/abs/case/charts/t-now-trend.png"},
            # Table-style KP — no image, but viz.kind == "table".
            {"topic": "tiny_summary", "captured_at_turn": "t-now",
             "viz": {"kind": "table"},
             "numbers": [{"m": "2025-05", "v": 1}, {"m": "2025-06", "v": 2}]},
            # No image AND not a table → still filtered out (e.g. text-only
            # KPs from the distiller that didn't get charted).
            {"topic": "prose_only", "captured_at_turn": "t-now",
             "viz": {"kind": "trend"}},
        ],
    }
    charts = finalize._collect_turn_charts(kb, "t-now", "C1")
    topics = sorted(c["topic"] for c in charts)
    assert topics == ["tiny_summary", "trend"]
    trend = next(c for c in charts if c["topic"] == "trend")
    assert trend["url"].endswith("trend.png")
    table = next(c for c in charts if c["topic"] == "tiny_summary")
    assert table["url"] == ""  # no PNG; the row data flows via the SSE payload


def test_collect_turn_charts_handles_empty_or_invalid_kb():
    assert finalize._collect_turn_charts({}, "t1", "C") == []
    assert finalize._collect_turn_charts(None, "t1", "C") == []
    assert finalize._collect_turn_charts({"x": "not a list"}, "t1", "C") == []


def test_append_charts_to_answer_no_op_when_empty():
    assert server._append_charts_to_answer("hello", []) == "hello"
    assert server._append_charts_to_answer("", []) == ""
    assert server._append_charts_to_answer(None, []) == ""


# ── SSE chart payload enrichment via _find_kp ───────────────────────────────
#
# The chart SSE event the frontend consumes (conductor.py) blends
# _collect_turn_charts (URL + topic + specialist) with _find_kp (claim,
# source_call, viz) via finalize._build_chart_payload, which REGENERATES the
# Vega-Lite spec from viz + numbers (the spec is not stored on the KP). These
# tests pin the contract that the multi-variable kinds (`trend_dual`,
# `trend_grid`) reach the frontend with the right `kind` string AND a
# regenerated interactive spec. The detailed spec shape lives in
# test_viz_renderer; here we assert the payload-level contract.


def _kp(topic, kind, y_fields, numbers=None, turn="t-now"):
    """Minimal KP shape that _collect_turn_charts + _find_kp consume.

    NB: `vega_spec` is intentionally NOT stored on the KP — it is regenerated
    at emit time from `viz` + `numbers` by finalize._build_chart_payload. So
    the KP carries `numbers`, and payload tests assert the REGENERATED spec.
    """
    return {
        "topic": topic,
        "captured_at_turn": turn,
        "image_path": f"/abs/case/charts/{turn}-{topic}.png",
        "claim": f"{topic} claim",
        "source_call": f"summarize_trend('{topic}', ...)",
        "viz": {"kind": kind, "x_field": "period", "y_fields": y_fields},
        "numbers": numbers if numbers is not None else [],
    }


def test_find_kp_returns_latest_matching_topic_in_turn():
    """_find_kp matches on (specialist, topic, captured_at_turn) and returns
    the LATEST occurrence — chronological iteration means the last appended
    entry wins, mirroring _collect_turn_charts's dedup convention."""
    kb = {
        "modeling": [
            _kp("score_vs_dpd", "trend_dual", ["score", "dpd"],
                [{"period": "2024-10", "score": 700, "dpd": 1}]),
            # Different topic same turn — unrelated.
            _kp("other", "trend", ["value"], [{"period": "2024-10", "value": 1}]),
            # Same topic, same turn — should win (latest); distinct numbers.
            _kp("score_vs_dpd", "trend_dual", ["score", "dpd"],
                [{"period": "2024-11", "score": 720, "dpd": 3},
                 {"period": "2024-12", "score": 715, "dpd": 5}]),
            # Same topic, EARLIER turn — should NOT match.
            _kp("score_vs_dpd", "trend_dual", ["score", "dpd"],
                [], turn="t-prev"),
        ],
    }
    found = cache._find_kp(kb, "modeling", "score_vs_dpd", "t-now")
    assert found is not None
    # Latest in-turn entry wins (last appended for the same key).
    assert len(found["numbers"]) == 2
    assert found["numbers"][0]["period"] == "2024-11"


def test_find_kp_returns_none_when_no_match():
    kb = {"modeling": [_kp("a", "trend", ["v"])]}
    assert cache._find_kp(kb, "modeling", "missing", "t-now") is None
    assert cache._find_kp(kb, "other_spec", "a", "t-now") is None
    assert cache._find_kp({}, "modeling", "a", "t-now") is None
    assert cache._find_kp(None, "modeling", "a", "t-now") is None


def test_chart_payload_carries_trend_dual_kind_and_regenerated_spec():
    """End-to-end shape: build a KB with a `trend_dual` KP, run the same
    collect + payload-build logic the conductor runs before
    sess.emit('chart', ...), and verify the payload carries both `kind ==
    'trend_dual'` AND a REGENERATED Vega-Lite spec with `resolve.scale.y ==
    'independent'` (the signal it's a dual-axis chart). The spec is rebuilt
    from viz+numbers — it is NOT stored on the KP."""
    kb = {"modeling": [
        _kp("score_vs_dpd", "trend_dual", ["score", "dpd"],
            [{"period": "2024-10", "score": 700, "dpd": 1},
             {"period": "2024-11", "score": 720, "dpd": 0},
             {"period": "2024-12", "score": 690, "dpd": 4},
             {"period": "2025-01", "score": 710, "dpd": 2}])
    ]}

    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-A")
    assert len(charts) == 1
    c = charts[0]
    kp = cache._find_kp(kb, c["specialist"], c["topic"], "t-now")
    payload = finalize._build_chart_payload(kp, c)

    assert payload["kind"] == "trend_dual"
    assert payload["url"].endswith("t-now-score_vs_dpd.png")
    assert payload["claim"] == "score_vs_dpd claim"
    # Frontend's interactive-renderer contract: a regenerated dual-axis spec
    # with independent y-scales and two line groups. Deep shape is pinned in
    # test_viz_renderer.
    assert payload["vega_spec"] is not None
    assert payload["vega_spec"]["resolve"]["scale"]["y"] == "independent"
    assert len(payload["vega_spec"]["layer"]) == 2


def test_chart_payload_carries_trend_grid_kind_and_vconcat_spec():
    """Same end-to-end check for `trend_grid` — the payload kind reaches
    the frontend as 'trend_grid' AND the regenerated vega_spec is a
    `vconcat` of N single-series panels sharing the x-axis."""
    kb = {"modeling": [
        _kp("credit_risk_panel", "trend_grid", ["tsr", "cdss", "txn_count"],
            [{"period": "2024-10", "tsr": 700, "cdss": 680, "txn_count": 42},
             {"period": "2024-11", "tsr": 720, "cdss": 690, "txn_count": 47}])
    ]}

    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-B")
    c = charts[0]
    kp = cache._find_kp(kb, c["specialist"], c["topic"], "t-now")
    payload = finalize._build_chart_payload(kp, c)

    assert payload["kind"] == "trend_grid"
    # Regenerated spec is a vconcat of one panel per y_field. Deep panel
    # shape (x/y encodings, ordering) is pinned in test_viz_renderer.
    assert payload["vega_spec"] is not None
    assert isinstance(payload["vega_spec"]["vconcat"], list)
    assert len(payload["vega_spec"]["vconcat"]) == 3


def test_chart_payload_kind_string_unknown_falls_back_to_empty():
    """Defensive: if a KP somehow lacks a `viz` block (legacy data, distiller
    edge case), the enrichment path returns kind='' rather than crashing.
    The frontend should treat empty kind as 'just show the PNG'."""
    kb = {"modeling": [{
        "topic": "legacy", "captured_at_turn": "t",
        "image_path": "/abs/charts/t-legacy.png",
        # No `viz`, no `numbers`.
    }]}
    charts = finalize._collect_turn_charts(kb, "t", "C")
    kp = cache._find_kp(kb, "modeling", "legacy", "t")
    payload = finalize._build_chart_payload(kp, charts[0])
    assert payload["kind"] == ""
    # No viz → nothing to regenerate → no interactive spec.
    assert payload["vega_spec"] is None
    # Charts still emit — the PNG path is the fallback when interactive
    # rendering isn't possible.
    assert charts[0]["url"].endswith(".png")


# ── Flask route: chart serving with path-traversal guard ────────────────────


def test_get_chart_route_rejects_traversal_attempts():
    """The chart route must NOT serve files outside the case folder. We
    accept 404 (our own path-traversal guard fired) or 308 (Werkzeug
    URL-normalized a malformed path before it reached our handler) —
    both mean the attack didn't reach the filesystem.
    """
    client = server.app.test_client()
    for bad in ["..%2Fetc%2Fpasswd", "../../etc/passwd", "/etc/passwd",
                "evil\\.png"]:
        rsp = client.get(f"/api/cases/CASE-1/charts/{bad}")
        assert rsp.status_code in (404, 308), (
            f"path {bad!r} must not be served — got {rsp.status_code}"
        )
        # Even on 308 (Werkzeug-redirect), the redirect target must not
        # resolve to a real file outside the case folder.
        assert b"PNG" not in rsp.data


def test_get_chart_route_returns_404_when_directory_missing():
    """No case folder yet → 404 (not 500)."""
    client = server.app.test_client()
    rsp = client.get("/api/cases/NEVER-CREATED-CASE/charts/anything.png")
    assert rsp.status_code == 404


def test_get_chart_route_serves_existing_png(tmp_path, monkeypatch):
    """Happy path — a real PNG under reports/<case>/charts/ is served with
    image/png content type."""
    # Redirect the server's reports dir to a tmp location and create a case
    # folder + PNG inside it.
    case_id = "CASE-VIZ-TEST"
    fake_reports = tmp_path / "reports"
    charts_dir = fake_reports / case_id / "charts"
    charts_dir.mkdir(parents=True)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"  # PNG magic header — enough for content sniffing
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    )
    (charts_dir / "demo.png").write_bytes(png_bytes)

    monkeypatch.setattr(server, "_REPORTS_DIR", fake_reports)
    client = server.app.test_client()
    rsp = client.get(f"/api/cases/{case_id}/charts/demo.png")
    assert rsp.status_code == 200
    assert rsp.mimetype == "image/png"
    assert rsp.data.startswith(b"\x89PNG")


# ── _detect_missing_reanswers (Round 2.5 protocol enforcement) ──────────────


def _gs_call(payload: dict | None) -> dict:
    """Construct a tool_calls entry for general_specialist with the given
    parsed payload (the ReviewReport dict)."""
    return {"call_id": "gs", "tool": "general_specialist",
            "sub_question": "review", "payload": payload}


def _spec_call(name: str) -> dict:
    return {"call_id": f"c-{name}", "tool": name, "sub_question": "..."}


def test_detect_missing_reanswers_flags_uncorrected_specialist():
    """general_specialist flagged modeling for correction, but no
    re-invocation of modeling appears AFTER → violation."""
    tool_calls = [
        _spec_call("bureau"),
        _spec_call("modeling"),
        _gs_call({
            "resolved": [{
                "pair": ["bureau", "modeling"],
                "corrected_specialist": "modeling",
                "corrected_value": "2024-12",
                "contradiction": "default date mismatch",
            }],
        }),
        # No re-invocation of modeling after the general_specialist call.
    ]
    out = server._detect_missing_reanswers(tool_calls)
    assert len(out) == 1
    assert out[0]["corrected_specialist"] == "modeling"
    assert out[0]["corrected_value"] == "2024-12"


def test_detect_missing_reanswers_satisfied_when_specialist_re_invoked():
    """A tool call to the corrected specialist AFTER general_specialist
    satisfies the protocol — no violation."""
    tool_calls = [
        _spec_call("bureau"),
        _spec_call("modeling"),
        _gs_call({
            "resolved": [{
                "pair": ["bureau", "modeling"],
                "corrected_specialist": "modeling",
                "corrected_value": "2024-12",
            }],
        }),
        # Round 2.5 re-invocation:
        _spec_call("modeling"),
    ]
    out = server._detect_missing_reanswers(tool_calls)
    assert out == []


def test_detect_missing_reanswers_ignores_resolutions_without_correction():
    """When a Resolution doesn't set corrected_specialist (or sets it to
    null / empty), no re-answer is required — no violation."""
    tool_calls = [
        _spec_call("bureau"),
        _spec_call("modeling"),
        _gs_call({
            "resolved": [
                {"pair": ["bureau", "modeling"], "corrected_specialist": None},
                {"pair": ["bureau", "modeling"], "corrected_specialist": ""},
                {"pair": ["bureau", "modeling"]},  # field absent
            ],
        }),
    ]
    out = server._detect_missing_reanswers(tool_calls)
    assert out == []


def test_detect_missing_reanswers_pre_general_calls_dont_count():
    """A tool call to the corrected specialist BEFORE general_specialist
    (i.e., the original Round 1 call) does NOT satisfy the re-answer
    requirement — only calls AFTER general_specialist count."""
    tool_calls = [
        _spec_call("bureau"),
        _spec_call("modeling"),  # ← Round 1 call
        _gs_call({
            "resolved": [{
                "corrected_specialist": "modeling",
                "corrected_value": "2024-12",
            }],
        }),
        # No modeling call after general_specialist.
    ]
    out = server._detect_missing_reanswers(tool_calls)
    assert len(out) == 1
    assert out[0]["corrected_specialist"] == "modeling"


def test_detect_missing_reanswers_handles_missing_or_malformed_payload():
    """Defensive: general_specialist's payload may be absent (streaming
    completed without it) or malformed — no false positives, no errors."""
    # Payload missing entirely.
    assert server._detect_missing_reanswers([_gs_call(None)]) == []
    # Payload not a dict.
    assert server._detect_missing_reanswers([{"tool": "general_specialist",
                                              "payload": "garbage"}]) == []
    # `resolved` not a list.
    assert server._detect_missing_reanswers([_gs_call({"resolved": "oops"})]) == []
    # Empty tool_calls.
    assert server._detect_missing_reanswers([]) == []
    # general_specialist with empty resolved list.
    assert server._detect_missing_reanswers([_gs_call({"resolved": []})]) == []


def test_append_charts_to_answer_appends_supporting_section():
    charts = [
        {"topic": "monthly_trend", "url": "/api/cases/X/charts/t-trend.png",
         "specialist": "spend_payments"},
        {"topic": "merchants", "url": "/api/cases/X/charts/t-merchants.png",
         "specialist": "spend_payments"},
    ]
    out = server._append_charts_to_answer("Spend rose 4×.", charts)
    assert "Spend rose 4×." in out
    assert "**Supporting charts**" in out
    assert "![monthly_trend](/api/cases/X/charts/t-trend.png)" in out
    assert "![merchants](/api/cases/X/charts/t-merchants.png)" in out
    # Section divider keeps charts visually distinct from the prose answer.
    assert "---" in out


def test_chart_payload_carries_table_kind_and_numbers():
    """End-to-end shape: a `kind='table'` KP (no image_path, but viz.kind
    == 'table') must produce a chart payload with `kind == 'table'`,
    a populated `numbers` list, and an empty `url` (no PNG rendered).
    This mirrors server.py:1703-1724 — the same logic run after the
    KB drain each turn."""
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    kb = {
        "spend_payments": [{
            "topic": "march_declines",
            "captured_at_turn": "t-now",
            # No image_path — table KPs skip the renderer.
            "claim": "Two declined transactions in March.",
            "source_call": "query_table('model_scores_transaction', ...)",
            "numbers": rows,
            "viz": {"kind": "table", "x_field": "a", "y_fields": []},
        }]
    }

    # Collect, then build the payload via the same helper the conductor runs.
    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-T")
    assert len(charts) == 1
    c = charts[0]
    kp = cache._find_kp(kb, c["specialist"], c["topic"], "t-now")
    payload = finalize._build_chart_payload(kp, c)

    assert payload["kind"] == "table"
    assert payload["url"] == ""          # no PNG; row data flows via numbers
    assert payload["numbers"] == rows    # row data present in payload
    assert payload["x_field"] == "a"
    assert payload["y_fields"] == []     # empty y_fields accepted (frontend derives cols)
    assert payload["claim"] == "Two declined transactions in March."
    # No vega_spec for table kind — frontend renders HTML, not vega-embed.
    assert payload.get("vega_spec") is None


def test_chart_payload_survives_a_later_distiller_kp_on_the_same_topic():
    """A distiller KP appended AFTER the specialist's `make_chart` KP for the
    same topic must not strip the chart out of the payload.

    Regression, case 11854808010 turn 3dc0b6d549d8: `make_chart` wrote a
    `kind='table'` KP for "Returned Payments…", then the auto-distiller
    appended its own KP for the same finding with NO `viz` and no image.
    `_collect_turn_charts` filters to chart-backing KPs so it kept the
    table, but `_find_kp` was a plain latest-wins scan and returned the
    distiller's — so the payload went out with `kind: ""`, no `numbers`
    and `url: ""`, and the Plots panel rendered `<img src="">`: a broken
    image where a one-row table belonged.
    """
    rows = [{"payment_date": "2025-04-28", "amount": 105818.60}]
    kb = {
        "spend_payments": [
            # 1. The specialist's explicit chart — the renderable KP.
            {
                "topic": "Returned Payments (Return Flag = 1)",
                "captured_at_turn": "t-now",
                "claim": "One returned payment: 2025-04-28, $105,818.60.",
                "source_call": "query_table('payments', ...)",
                "numbers": rows,
                "viz": {"kind": "table", "x_field": "payment_date",
                        "y_fields": ["amount"]},
            },
            # 2. The distiller's KP for the SAME topic, appended later.
            #    No viz, no image_path — it backs no chart.
            {
                "topic": "Returned Payments (Return Flag = 1)",
                "captured_at_turn": "t-now",
                "claim": "There was one returned payment in the history.",
                "source_call": "",
                "numbers": [],
            },
        ]
    }

    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-R")
    assert len(charts) == 1
    c = charts[0]
    kp = cache._find_kp(kb, c["specialist"], c["topic"], "t-now")
    payload = finalize._build_chart_payload(kp, c)

    # The renderable KP wins, so the panel gets a table — not a broken <img>.
    assert payload["kind"] == "table"
    assert payload["numbers"] == rows
    assert payload["x_field"] == "payment_date"
    assert payload["y_fields"] == ["amount"]


def test_find_kp_still_returns_latest_when_no_kp_backs_a_chart():
    """The chart-backing preference is a tie-break, not a filter: when no
    KP for the topic backs a chart, `_find_kp` keeps its plain latest-wins
    behaviour so non-chart callers are unaffected."""
    kb = {
        "modeling": [
            {"topic": "a", "captured_at_turn": "t", "claim": "first"},
            {"topic": "a", "captured_at_turn": "t", "claim": "second"},
        ]
    }
    found = cache._find_kp(kb, "modeling", "a", "t")
    assert found is not None and found["claim"] == "second"


# ── RC2: failure branches must replay completed specialists' traces ──────────


class _RecordingSess:
    """Minimal session stand-in capturing emitted SSE events."""
    def __init__(self):
        self.events = []

    def emit(self, name, payload):
        self.events.append((name, payload))


def test_replay_completed_specialists_emits_team_plan_and_agent_completed():
    sess = _RecordingSess()
    tool_calls = [
        {"call_id": "c1", "tool": "modeling",
         "payload": {"findings": "ok"}, "duration_ms": 120},
        {"call_id": "c2", "tool": "report_agent"},  # no payload → skipped
    ]
    finalize._replay_completed_specialists(sess, "t1", tool_calls)

    names = [n for n, _ in sess.events]
    assert names == ["team_plan", "agent_completed"]
    ac = [p for n, p in sess.events if n == "agent_completed"]
    assert len(ac) == 1  # only the specialist with a payload
    assert ac[0]["tool"] == "modeling"
    assert ac[0]["payload"] == {"findings": "ok"}
    assert ac[0]["turn_id"] == "t1"


def test_replay_completed_specialists_noop_when_none_completed():
    sess = _RecordingSess()
    finalize._replay_completed_specialists(sess, "t1", [{"call_id": "c1", "tool": "x"}])
    assert sess.events == []


# ── replay-on-reconnect: emit() buffers events for late subscribers ──────────


def _bare_session():
    """A CaseSession with only the attrs emit() touches; the rest are dummies."""
    return server.CaseSession(
        case_id="c", gateway=None, catalog=None, clients=None,
        pillar_yaml={}, chat_agent=None, logger=None,
    )


def test_emit_buffers_events_but_skips_pings():
    sess = _bare_session()
    sess.emit("turn_started", {"turn_id": "t1"})
    sess.emit("agent_completed", {"turn_id": "t1", "tool": "modeling"})
    sess.emit("ping", {})  # keepalive — must NOT be buffered for replay
    names = [n for n, _ in sess.event_buffer]
    assert names == ["turn_started", "agent_completed"]


def test_emit_still_fans_out_to_subscribers():
    import queue as _q
    sess = _bare_session()
    q = _q.Queue()
    sess.subscribers.append(q)
    sess.emit("final", {"turn_id": "t1", "answer": "x"})
    assert q.get_nowait() == ("final", {"turn_id": "t1", "answer": "x"})
    assert list(sess.event_buffer) == [("final", {"turn_id": "t1", "answer": "x"})]


def test_event_buffer_is_bounded():
    sess = _bare_session()
    for i in range(server._EVENT_BUFFER_MAX + 50):
        sess.emit("agent_completed", {"turn_id": "t", "i": i})
    assert len(sess.event_buffer) == server._EVENT_BUFFER_MAX
    # Oldest frames dropped; newest retained.
    assert sess.event_buffer[-1][1]["i"] == server._EVENT_BUFFER_MAX + 49


# ── SSE non-finite-float serialization ──────────────────────────────────────


def _strict_loads(s):
    """Parse like a browser's JSON.parse: REJECT bare NaN/Infinity tokens.

    Python's json.loads accepts them by default; the browser does not. We force
    the strict behavior so the test catches frames that would silently drop on
    the client.
    """
    import json as _json

    def _reject(tok):
        raise ValueError(f"non-finite JSON token: {tok}")

    return _json.loads(s, parse_constant=_reject)


def test_sse_data_sanitizes_non_finite_floats():
    """A `chart` payload carrying NaN/Infinity (in a vega spec, a numeric cell,
    or a computed stat) must serialize to VALID JSON. Python emits bare
    NaN/Infinity tokens, which the browser's JSON.parse rejects — silently
    dropping the whole `chart` event. Non-finite floats become null instead.
    """
    payload = {
        "topic": "tsr_cdss",
        "vega_spec": {"values": [{"y": float("nan")}, {"y": 1.5}]},
        "numbers": [{"v": float("inf")}],
        "ratio": float("-inf"),
        "ok": 2.0,
        "n": 3,
        "claim": "fine",
    }
    s = server._sse_data(payload)
    parsed = _strict_loads(s)  # must NOT raise (no bare NaN/Infinity on the wire)
    assert parsed["vega_spec"]["values"][0]["y"] is None
    assert parsed["vega_spec"]["values"][1]["y"] == 1.5
    assert parsed["numbers"][0]["v"] is None
    assert parsed["ratio"] is None
    assert parsed["ok"] == 2.0
    assert parsed["n"] == 3
    assert parsed["claim"] == "fine"


def test_sse_data_finite_payload_roundtrips_unchanged():
    payload = {"a": 1, "b": 2.5, "c": "x", "d": [1, 2, {"e": True}], "f": None}
    assert _strict_loads(server._sse_data(payload)) == payload


# ── Round-1 planning watchdog (_next_planning_event) ─────────────────────────

import asyncio  # noqa: E402
import time as _time  # noqa: E402
import pytest  # noqa: E402


class _SlowAiter:
    """Single-shot async iterator that sleeps `delay` before yielding once."""

    def __init__(self, delay, value="event"):
        self._delay = delay
        self._value = value
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        await asyncio.sleep(self._delay)
        self._done = True
        return self._value


async def test_planning_event_raises_on_stall():
    """A round-1 call that exceeds the planning budget raises _PlanningTimeout."""
    it = _SlowAiter(delay=5.0).__aiter__()
    deadline = _time.monotonic() + 0.05  # 50ms budget vs 5s "call"
    with pytest.raises(conductor._PlanningTimeout):
        await conductor._next_planning_event(it, deadline, first_tool_seen=False)


async def test_planning_event_returns_when_fast():
    it = _SlowAiter(delay=0.0).__aiter__()
    deadline = _time.monotonic() + 1.0
    ev = await conductor._next_planning_event(it, deadline, first_tool_seen=False)
    assert ev == "event"


async def test_planning_deadline_disarmed_after_first_tool():
    """Once the first tool call has landed, a slow event must NOT time out even
    with an already-expired deadline — specialists run under the turn fence."""
    it = _SlowAiter(delay=0.05).__aiter__()
    expired = _time.monotonic() - 10
    ev = await conductor._next_planning_event(it, expired, first_tool_seen=True)
    assert ev == "event"


async def test_planning_event_no_deadline_passes_through():
    """Final attempt (plan_deadline=None) never times out the planning phase."""
    it = _SlowAiter(delay=0.05).__aiter__()
    ev = await conductor._next_planning_event(it, None, first_tool_seen=False)
    assert ev == "event"


async def test_planning_event_propagates_stop_async_iteration():
    it = _SlowAiter(delay=0.0).__aiter__()
    await it.__anext__()  # exhaust it
    deadline = _time.monotonic() + 1.0
    with pytest.raises(StopAsyncIteration):
        await conductor._next_planning_event(it, deadline, first_tool_seen=False)


# ── Case-id canonicalization at the HTTP door ───────────────────────────────


def test_url_preprocessor_canonicalizes_case_id_for_every_route():
    """The `<case_id>` path segment is normalized once, for all routes, so no
    handler has to remember. Handlers use it as BOTH the session key and a
    path component (`reports/<case_id>/charts`), so a padded id forks one
    case across two directories."""
    values = {"case_id": "11854808010 ", "filename": "t-chart.png"}
    server._canonicalize_case_id("get_chart", values)
    assert values["case_id"] == "11854808010"
    # Other view args are untouched.
    assert values["filename"] == "t-chart.png"


def test_url_preprocessor_ignores_routes_without_a_case_id():
    """`/api/cases` and any non-case route pass through unharmed, including
    the `values is None` shape Flask hands to static endpoints."""
    values: dict = {}
    server._canonicalize_case_id("get_cases", values)
    assert values == {}
    server._canonicalize_case_id("static", None)  # must not raise


def test_get_or_create_session_normalizes_for_non_http_callers():
    """Prewarm, tests and other in-process callers bypass the preprocessor,
    so the session chokepoint normalizes too — otherwise SESSIONS holds two
    entries (two logs, two report dirs) for one case."""
    import pytest
    # An unknown case raises KeyError; the point is WHICH id it reports —
    # the normalized one, proving the strip happened before the lookup.
    with pytest.raises(KeyError) as exc:
        server._get_or_create_session("definitely-not-a-case ")
    assert "definitely-not-a-case" in str(exc.value)
    assert "definitely-not-a-case " not in str(exc.value)
