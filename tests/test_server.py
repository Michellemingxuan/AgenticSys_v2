"""Tests for server.py helpers that are testable without booting the Flask app.

Importing server.py runs heavy bootstrap (data source resolution, catalog,
firewall stack), so we only target pure helpers — e.g., the input_history
pruner that bounds orchestrator memory growth across turns.
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


def _user(content):
    return {"role": "user", "content": content}


def _tool_call(call_id, tool):
    return {"type": "function_call", "name": tool, "call_id": call_id, "arguments": "{}"}


def _tool_output(call_id, output):
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _assistant(content):
    return {"role": "assistant", "content": content}


def test_prune_input_history_noop_when_only_recent_turns():
    """With ≤ keep_recent_turns user messages, nothing should change."""
    history = [
        _user("turn 1 q"),
        _tool_call("c1", "spend_payments"),
        _tool_output("c1", "x" * 5000),  # large but recent
        _assistant("turn 1 answer"),
    ]
    pruned, stats = server._prune_input_history(history, keep_recent_turns=2)
    assert pruned == history
    assert stats["items_elided"] == 0
    assert stats["bytes_saved"] == 0


def test_prune_input_history_elides_old_tool_outputs_only():
    """Older turn's `function_call_output` items get a stub; assistant
    messages and the function_call records themselves are preserved."""
    big_payload = "x" * 5000
    history = [
        # Turn 1 (oldest — should be pruned)
        _user("turn 1"),
        _tool_call("c1", "spend_payments"),
        _tool_output("c1", big_payload),
        _assistant("turn 1 answer"),
        # Turn 2 (kept)
        _user("turn 2"),
        _tool_call("c2", "modeling"),
        _tool_output("c2", "y" * 3000),
        _assistant("turn 2 answer"),
        # Turn 3 (kept)
        _user("turn 3"),
        _tool_call("c3", "bureau"),
        _tool_output("c3", "z" * 2000),
        _assistant("turn 3 answer"),
    ]
    pruned, stats = server._prune_input_history(history, keep_recent_turns=2)

    # Only the turn-1 tool output is elided.
    assert stats["items_elided"] == 1
    assert stats["bytes_saved"] > 4000  # ~5000 - len(stub)
    assert len(pruned) == len(history)  # length preserved (stub replaces, not drops)

    # Turn 1: tool_call survives, tool_output replaced with the stub
    assert pruned[1] == history[1]               # function_call kept
    assert pruned[2]["type"] == "function_call_output"
    assert pruned[2]["output"] == server._ELIDED_TOOL_OUTPUT
    assert pruned[2]["call_id"] == "c1"
    assert pruned[3] == history[3]               # turn 1 assistant kept

    # Turns 2 + 3: untouched verbatim.
    for i in range(4, 12):
        assert pruned[i] == history[i]


def test_prune_input_history_idempotent():
    """Re-pruning an already-pruned list must be a no-op (the stub equals
    `_ELIDED_TOOL_OUTPUT`, so the elision check skips it on the second pass)."""
    history = [
        _user("t1"), _tool_call("c1", "x"),
        _tool_output("c1", "big" * 1000), _assistant("a1"),
        _user("t2"), _tool_call("c2", "y"),
        _tool_output("c2", "big" * 1000), _assistant("a2"),
        _user("t3"), _tool_call("c3", "z"),
        _tool_output("c3", "small"), _assistant("a3"),
    ]
    once, stats1 = server._prune_input_history(history, keep_recent_turns=2)
    twice, stats2 = server._prune_input_history(once, keep_recent_turns=2)
    assert once == twice
    assert stats2["items_elided"] == 0


def test_prune_input_history_handles_unknown_shapes():
    """Items that don't match the SDK's known item shapes pass through
    unchanged — defensive behavior so the pruner can't accidentally drop
    content if the SDK introduces new item kinds."""
    weird_item = {"some_future_kind": "unknown payload"}
    history = [
        _user("t1"), _tool_call("c1", "x"), _tool_output("c1", "big" * 500),
        weird_item, _assistant("a1"),
        _user("t2"), _user("t3"),  # 3 user messages → t1 is "old"
    ]
    pruned, _ = server._prune_input_history(history, keep_recent_turns=2)
    # The unknown item is pre-cutoff but doesn't match function_call_output,
    # so it stays as-is.
    assert weird_item in pruned


def test_prune_input_history_empty_or_invalid_input():
    """Empty / non-list inputs return unchanged with zero stats."""
    pruned, stats = server._prune_input_history([], keep_recent_turns=2)
    assert pruned == []
    assert stats["items_elided"] == 0

    # Non-list input is a defensive case — we just return it unmodified.
    pruned, stats = server._prune_input_history("not a list", keep_recent_turns=2)
    assert pruned == "not a list"
    assert stats["items_elided"] == 0


# ── Phase 3 — KB-warmth hint ────────────────────────────────────────────────


def test_format_kb_warmth_hint_lists_warm_specialists():
    kb = {
        "spend_payments": [{"topic": "a", "claim": "x"},
                           {"topic": "b", "claim": "y"},
                           {"topic": "c", "claim": "z"}],
        "modeling": [{"topic": "d", "claim": "w"},
                     {"topic": "e", "claim": "v"}],
        "bureau": [],  # empty → must NOT appear in the hint
    }
    hint = server._format_kb_warmth_hint(kb)
    assert hint.startswith("[KB-warmth:")
    assert "spend_payments (3 KPs)" in hint
    assert "modeling (2 KPs)" in hint
    assert "bureau" not in hint
    # Sorted by KP count, descending.
    assert hint.index("spend_payments") < hint.index("modeling")
    # Singular form for n=1.
    kb_one = {"x": [{"topic": "t", "claim": "c"}]}
    assert "1 KP" in server._format_kb_warmth_hint(kb_one)
    assert "1 KPs" not in server._format_kb_warmth_hint(kb_one)


def test_format_kb_warmth_hint_empty_when_no_warm_specialists():
    """Empty KB or all-empty values → empty hint, so the orchestrator's
    prompt isn't cluttered on the first turn or after /rewind."""
    assert server._format_kb_warmth_hint({}) == ""
    assert server._format_kb_warmth_hint({"x": [], "y": []}) == ""
    assert server._format_kb_warmth_hint(None) == ""


# ── Bounded session memory ──────────────────────────────────────────────────


def test_store_cached_qa_evicts_oldest_entry(monkeypatch):
    monkeypatch.setattr(server, "_QA_CACHE_MAX_ENTRIES", 2)
    sess = SimpleNamespace(qa_cache={})

    server._store_cached_qa(sess, "q1", {"answer": "a1"})
    server._store_cached_qa(sess, "q2", {"answer": "a2"})
    # Touch q1 so q2 becomes the least-recently used entry.
    assert server._get_cached_qa(sess, "q1")["answer"] == "a1"
    evicted = server._store_cached_qa(sess, "q3", {"answer": "a3"})

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
    charts = server._collect_turn_charts(kb, "t-now", "CASE-1")
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
    charts = server._collect_turn_charts(kb, "t-now", "C1")
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
    charts = server._collect_turn_charts(kb, "t", "C")
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
    charts = server._collect_turn_charts(kb, "t-now", "C1")
    topics = sorted(c["topic"] for c in charts)
    assert topics == ["tiny_summary", "trend"]
    trend = next(c for c in charts if c["topic"] == "trend")
    assert trend["url"].endswith("trend.png")
    table = next(c for c in charts if c["topic"] == "tiny_summary")
    assert table["url"] == ""  # no PNG; the row data flows via the SSE payload


def test_collect_turn_charts_handles_empty_or_invalid_kb():
    assert server._collect_turn_charts({}, "t1", "C") == []
    assert server._collect_turn_charts(None, "t1", "C") == []
    assert server._collect_turn_charts({"x": "not a list"}, "t1", "C") == []


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
    found = server._find_kp(kb, "modeling", "score_vs_dpd", "t-now")
    assert found is not None
    # Latest in-turn entry: vega_spec has the non-empty layer list.
    assert len(found["vega_spec"]["layer"]) == 2


def test_find_kp_returns_none_when_no_match():
    kb = {"modeling": [_kp("a", "trend", ["v"], {"mark": "line"})]}
    assert server._find_kp(kb, "modeling", "missing", "t-now") is None
    assert server._find_kp(kb, "other_spec", "a", "t-now") is None
    assert server._find_kp({}, "modeling", "a", "t-now") is None
    assert server._find_kp(None, "modeling", "a", "t-now") is None


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
    charts = server._collect_turn_charts(kb, "t-now", "CASE-A")
    assert len(charts) == 1
    c = charts[0]
    kp = server._find_kp(kb, c["specialist"], c["topic"], "t-now")
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

    charts = server._collect_turn_charts(kb, "t-now", "CASE-B")
    c = charts[0]
    kp = server._find_kp(kb, c["specialist"], c["topic"], "t-now")
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
    charts = server._collect_turn_charts(kb, "t", "C")
    kp = server._find_kp(kb, "modeling", "legacy", "t")
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
