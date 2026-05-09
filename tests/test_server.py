"""Tests for server.py helpers that are testable without booting the Flask app.

Importing server.py runs heavy bootstrap (data source resolution, catalog,
firewall stack), so we only target pure helpers — e.g., the input_history
pruner that bounds orchestrator memory growth across turns.
"""

import os
import sys

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


def test_collect_turn_charts_handles_empty_or_invalid_kb():
    assert server._collect_turn_charts({}, "t1", "C") == []
    assert server._collect_turn_charts(None, "t1", "C") == []
    assert server._collect_turn_charts({"x": "not a list"}, "t1", "C") == []


def test_append_charts_to_answer_no_op_when_empty():
    assert server._append_charts_to_answer("hello", []) == "hello"
    assert server._append_charts_to_answer("", []) == ""
    assert server._append_charts_to_answer(None, []) == ""


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
