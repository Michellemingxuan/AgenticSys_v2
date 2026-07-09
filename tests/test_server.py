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


def test_collect_turn_charts_does_not_dedupe_across_specialists():
    """Two specialists charting the same topic in the same turn → both
    appear (different `(specialist, topic)` keys)."""
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
# The chart SSE event the frontend consumes (server.py ~line 1115) blends
# _collect_turn_charts (URL + topic + specialist) with _find_kp (claim,
# source_call, kind, vega_spec). These tests pin the contract that the new
# multi-variable kinds (`trend_dual`, `trend_grid`) reach the frontend with
# the right `kind` string AND the right `vega_spec` shape (layered+independent
# y resolve, or vconcat) — i.e., everything an interactive Vega-Lite renderer
# needs to reproduce the chart from the PNG-free spec.


def _kp(topic, kind, y_fields, vega_spec, turn="t-now"):
    """Minimal KP shape that _collect_turn_charts + _find_kp consume."""
    return {
        "topic": topic,
        "captured_at_turn": turn,
        "image_path": f"/abs/case/charts/{turn}-{topic}.png",
        "claim": f"{topic} claim",
        "source_call": f"summarize_trend('{topic}', ...)",
        "viz": {"kind": kind, "x_field": "period", "y_fields": y_fields},
        "vega_spec": vega_spec,
    }


def test_find_kp_returns_latest_matching_topic_in_turn():
    """_find_kp matches on (specialist, topic, captured_at_turn) and returns
    the LATEST occurrence — chronological iteration means the last appended
    entry wins, mirroring _collect_turn_charts's dedup convention."""
    kb = {
        "modeling": [
            _kp("score_vs_dpd", "trend_dual", ["score", "dpd"],
                {"layer": [], "resolve": {"scale": {"y": "independent"}}}),
            # Different topic same turn — unrelated.
            _kp("other", "trend", ["value"], {"mark": "line"}),
            # Same topic, same turn — should win (latest).
            _kp("score_vs_dpd", "trend_dual", ["score", "dpd"],
                {"layer": [{"mark": "line"}, {"mark": "line"}],
                 "resolve": {"scale": {"y": "independent"}}}),
            # Same topic, EARLIER turn — should NOT match.
            _kp("score_vs_dpd", "trend_dual", ["score", "dpd"],
                {"layer": []}, turn="t-prev"),
        ],
    }
    found = cache._find_kp(kb, "modeling", "score_vs_dpd", "t-now")
    assert found is not None
    # Latest in-turn entry: vega_spec has the non-empty layer list.
    assert len(found["vega_spec"]["layer"]) == 2


def test_find_kp_returns_none_when_no_match():
    kb = {"modeling": [_kp("a", "trend", ["v"], {"mark": "line"})]}
    assert cache._find_kp(kb, "modeling", "missing", "t-now") is None
    assert cache._find_kp(kb, "other_spec", "a", "t-now") is None
    assert cache._find_kp({}, "modeling", "a", "t-now") is None
    assert cache._find_kp(None, "modeling", "a", "t-now") is None


def test_chart_payload_carries_trend_dual_kind_and_layered_spec():
    """End-to-end shape: build a KB with a `trend_dual` KP, run the same
    collect+enrich logic the server runs before sess.emit('chart', ...),
    and verify the payload the frontend receives carries both `kind ==
    'trend_dual'` AND a Vega-Lite spec with `resolve.scale.y ==
    'independent'`. This is what tells the frontend it's looking at a
    dual-axis chart."""
    vega_spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": [{"period": "2024-11", "score": 720, "dpd": 0}]},
        "layer": [
            {"mark": "line", "encoding": {
                "x": {"field": "period", "type": "ordinal"},
                "y": {"field": "score", "type": "quantitative"}}},
            {"mark": "line", "encoding": {
                "x": {"field": "period", "type": "ordinal"},
                "y": {"field": "dpd", "type": "quantitative"}}},
        ],
        "resolve": {"scale": {"y": "independent"}},
    }
    kb = {"modeling": [
        _kp("score_vs_dpd", "trend_dual", ["score", "dpd"], vega_spec)
    ]}

    # Mirror server.py:1107-1123 — collect, then enrich each chart with
    # the matching KP's metadata.
    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-A")
    assert len(charts) == 1
    c = charts[0]
    kp = cache._find_kp(kb, c["specialist"], c["topic"], "t-now")
    payload = {
        "specialist": c["specialist"],
        "topic": c["topic"],
        "url": c["url"],
        "claim": (kp or {}).get("claim", ""),
        "source_call": (kp or {}).get("source_call", ""),
        "kind": ((kp or {}).get("viz") or {}).get("kind", ""),
        "vega_spec": (kp or {}).get("vega_spec"),
    }

    assert payload["kind"] == "trend_dual"
    assert payload["url"].endswith("t-now-score_vs_dpd.png")
    assert payload["claim"] == "score_vs_dpd claim"
    # Frontend's interactive-renderer contract — these are the keys an
    # embed call (e.g. vega-embed) needs to render the dual-axis chart.
    assert payload["vega_spec"]["resolve"]["scale"]["y"] == "independent"
    assert len(payload["vega_spec"]["layer"]) == 2
    assert payload["vega_spec"]["layer"][0]["encoding"]["y"]["field"] == "score"
    assert payload["vega_spec"]["layer"][1]["encoding"]["y"]["field"] == "dpd"


def test_chart_payload_carries_trend_grid_kind_and_vconcat_spec():
    """Same end-to-end check for `trend_grid` — the payload kind reaches
    the frontend as 'trend_grid' AND the vega_spec is a `vconcat` of N
    single-series specs sharing the x-axis."""
    vega_spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": [
            {"period": "2024-11", "tsr": 720, "cdss": 680, "txn_count": 42}
        ]},
        "vconcat": [
            {"mark": "line", "encoding": {
                "x": {"field": "period", "type": "ordinal"},
                "y": {"field": "tsr", "type": "quantitative"}}},
            {"mark": "line", "encoding": {
                "x": {"field": "period", "type": "ordinal"},
                "y": {"field": "cdss", "type": "quantitative"}}},
            {"mark": "line", "encoding": {
                "x": {"field": "period", "type": "ordinal"},
                "y": {"field": "txn_count", "type": "quantitative"}}},
        ],
    }
    kb = {"modeling": [
        _kp("credit_risk_panel", "trend_grid",
            ["tsr", "cdss", "txn_count"], vega_spec)
    ]}

    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-B")
    c = charts[0]
    kp = cache._find_kp(kb, c["specialist"], c["topic"], "t-now")
    payload = {
        "specialist": c["specialist"],
        "topic": c["topic"],
        "url": c["url"],
        "kind": ((kp or {}).get("viz") or {}).get("kind", ""),
        "vega_spec": (kp or {}).get("vega_spec"),
    }

    assert payload["kind"] == "trend_grid"
    assert isinstance(payload["vega_spec"]["vconcat"], list)
    assert len(payload["vega_spec"]["vconcat"]) == 3
    # Each sub-spec shares the x-axis with the same field.
    for sub in payload["vega_spec"]["vconcat"]:
        assert sub["mark"] == "line"
        assert sub["encoding"]["x"]["field"] == "period"
    # Y fields appear in y_fields order — the panel order the frontend
    # renders top-to-bottom matches the specialist's y_fields ordering.
    y_fields = [sub["encoding"]["y"]["field"]
                for sub in payload["vega_spec"]["vconcat"]]
    assert y_fields == ["tsr", "cdss", "txn_count"]


def test_chart_payload_kind_string_unknown_falls_back_to_empty():
    """Defensive: if a KP somehow lacks a `viz` block (legacy data, distiller
    edge case), the enrichment path returns kind='' rather than crashing.
    The frontend should treat empty kind as 'just show the PNG'."""
    kb = {"modeling": [{
        "topic": "legacy", "captured_at_turn": "t",
        "image_path": "/abs/charts/t-legacy.png",
        # No `viz`, no `vega_spec`.
    }]}
    charts = finalize._collect_turn_charts(kb, "t", "C")
    kp = cache._find_kp(kb, "modeling", "legacy", "t")
    kind = ((kp or {}).get("viz") or {}).get("kind", "")
    vega = (kp or {}).get("vega_spec")
    assert kind == ""
    assert vega is None
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

    # Mirror server.py:1703-1724 — collect, then enrich each chart with
    # the matching KP's metadata (same logic as the trend_dual test above).
    charts = finalize._collect_turn_charts(kb, "t-now", "CASE-T")
    assert len(charts) == 1
    c = charts[0]
    kp = cache._find_kp(kb, c["specialist"], c["topic"], "t-now")
    viz = (kp or {}).get("viz") or {}
    payload = {
        "specialist": c["specialist"],
        "topic": c["topic"],
        "url": c["url"],
        "claim": (kp or {}).get("claim", ""),
        "source_call": (kp or {}).get("source_call", ""),
        "kind": viz.get("kind", "") if isinstance(viz, dict) else "",
        "vega_spec": (kp or {}).get("vega_spec"),
    }
    if payload["kind"] == "table":
        payload["numbers"] = (kp or {}).get("numbers") or []
        payload["x_field"] = viz.get("x_field", "") if isinstance(viz, dict) else ""
        payload["y_fields"] = viz.get("y_fields") or [] if isinstance(viz, dict) else []

    assert payload["kind"] == "table"
    assert payload["url"] == ""          # no PNG; row data flows via numbers
    assert payload["numbers"] == rows    # row data present in payload
    assert payload["x_field"] == "a"
    assert payload["y_fields"] == []     # empty y_fields accepted (frontend derives cols)
    assert payload["claim"] == "Two declined transactions in March."
    # No vega_spec for table kind — frontend renders HTML, not vega-embed.
    assert payload.get("vega_spec") is None


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
