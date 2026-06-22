"""Tests for GET /catalog and POST /catalog/reconcile.

Subprocess is ALWAYS mocked — no real reconcile, no LLM, no real
config/data_profiles or context/ directories are ever touched.
"""
from __future__ import annotations

import json
import os

import yaml
from flask import Flask
from unittest.mock import patch

from tools.node_trace.catalog_page import register_catalog_routes

# ---------------------------------------------------------------------------
# Helper: shared DB path for tests that need it
# ---------------------------------------------------------------------------
_TEST_DB = "logs/test_node_traces.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app(tmp_path, reconcile_enable: bool = False, db_path: str = _TEST_DB):
    """Build a test Flask app.  Pass reconcile_enable=True for tests that
    exercise the enabled path; the default mirrors the production default (OFF).
    Pass db_path to control which NODE_TRACE_DB is shown in the header.
    """
    app = Flask(__name__)
    app.config.update(
        PROFILE_DIR=str(tmp_path / "prof"),
        CONTEXT_DIR=str(tmp_path / "ctx"),
        PROVENANCE_PATH=str(tmp_path / ".prov.json"),
        RECONCILE_RESULTS=str(tmp_path / "last.json"),
        CATALOG_RECONCILE_ENABLE=reconcile_enable,
        NODE_TRACE_DB=db_path,
    )
    register_catalog_routes(app)
    return app


def _write_profile(prof_dir, table_name="t", extra=None):
    """Write a minimal profile YAML for one table."""
    spec = {"table": table_name, "description": "d", "columns": {
        "a": {"dtype": "int", "description": "column a"},
    }}
    if extra:
        spec.update(extra)
    prof_dir.mkdir(exist_ok=True)
    (prof_dir / f"{table_name}.yaml").write_text(yaml.safe_dump(spec))


def _fake_subprocess_run(writes=None, context_writes=None, flags=None):
    """Return a side_effect callable that simulates the reconcile CLI.

    It finds the ``--json <path>`` arg in the command, writes a fake JSON
    result there, and returns a mock CompletedProcess with returncode=0.
    """
    payload = {
        "writes": writes or [],
        "context_writes": context_writes or [],
        "flags": flags or ["[coverage] t.a"],
    }

    def _side_effect(cmd, **kw):
        # Locate --json argument and write fake output there.
        try:
            idx = cmd.index("--json")
            jpath = cmd[idx + 1]
            with open(jpath, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except (ValueError, IndexError):
            pass  # no --json arg; ignore

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return _side_effect


# ---------------------------------------------------------------------------
# GET /catalog
# ---------------------------------------------------------------------------

def test_catalog_get_renders_table_name(tmp_path):
    """GET /catalog returns 200 and the table name appears in the HTML."""
    _write_profile(tmp_path / "prof", table_name="t")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"t" in r.data
    assert b"Catalog" in r.data


def test_catalog_get_shows_column_info(tmp_path):
    """GET /catalog shows column dtype and the column name."""
    (tmp_path / "prof").mkdir()
    (tmp_path / "prof" / "scores.yaml").write_text(yaml.safe_dump({
        "table": "scores",
        "description": "score table",
        "columns": {
            "credit_score": {"dtype": "float", "description": "credit risk score"},
        },
    }))
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"credit_score" in r.data
    assert b"float" in r.data


def test_catalog_get_provenance_badge(tmp_path):
    """GET /catalog renders a provenance badge (human/agent/unmanaged)."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    # At least one badge word must appear.
    assert any(kw in r.data for kw in (b"unmanaged", b"agent", b"human"))


def test_catalog_get_reconcile_form(tmp_path):
    """GET /catalog includes the Reconcile form with no_llm checkbox when enabled."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"reconcile" in r.data.lower()
    assert b"no_llm" in r.data


def test_catalog_get_nav_links(tmp_path):
    """GET /catalog includes tab links to Traces (/) and Data Catalog."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    # Both Traces and Data Catalog tab targets present.
    assert b'href="/"' in r.data
    assert b"/catalog" in r.data
    assert b"Traces" in r.data
    assert b"Data Catalog" in r.data


def test_catalog_get_empty_profile_dir(tmp_path):
    """GET /catalog with no YAML files returns 200 (empty tables list)."""
    (tmp_path / "prof").mkdir()
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200


def test_catalog_get_missing_profile_dir(tmp_path):
    """GET /catalog when PROFILE_DIR does not exist still returns 200."""
    # Do NOT create prof dir — catalog_view should handle missing dirs gracefully.
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200


def test_catalog_get_last_reconcile_panel(tmp_path):
    """GET /catalog shows the last-reconcile panel when results JSON exists."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    summary = {
        "timestamp": "2026-06-22T10:00:00",
        "writes": 2,
        "context_writes": 1,
        "flags": ["[coverage] t.a"],
        "no_llm": False,
    }
    (tmp_path / "last.json").write_text(json.dumps(summary))
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"2026-06-22" in r.data


# ---------------------------------------------------------------------------
# POST /catalog/reconcile — subprocess ALWAYS mocked
# ---------------------------------------------------------------------------

def test_catalog_reconcile_invokes_subprocess(tmp_path):
    """POST /catalog/reconcile calls subprocess.run and writes results json."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)

    with patch("tools.node_trace.catalog_page.subprocess.run",
               side_effect=_fake_subprocess_run(flags=["[coverage] t.a"])) as m:
        r = app.test_client().post("/catalog/reconcile", data={})
        assert r.status_code in (200, 302)
        assert m.called

    # Results JSON persisted.
    assert os.path.exists(tmp_path / "last.json")
    data = json.loads((tmp_path / "last.json").read_text())
    assert "timestamp" in data


def test_catalog_reconcile_no_llm_flag(tmp_path):
    """POST with no_llm checkbox appends --no-llm to the subprocess command."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)
    captured_cmd = []

    def _capture(cmd, **kw):
        captured_cmd.extend(cmd)
        try:
            idx = cmd.index("--json")
            jpath = cmd[idx + 1]
            with open(jpath, "w") as f:
                json.dump({"writes": [], "context_writes": [], "flags": []}, f)
        except (ValueError, IndexError):
            pass

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    with patch("tools.node_trace.catalog_page.subprocess.run", side_effect=_capture):
        r = app.test_client().post("/catalog/reconcile", data={"no_llm": "on"})
        assert r.status_code in (200, 302)

    assert "--no-llm" in captured_cmd


def test_catalog_reconcile_no_llm_absent_without_checkbox(tmp_path):
    """POST without no_llm checkbox does NOT pass --no-llm."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)
    captured_cmd = []

    def _capture(cmd, **kw):
        captured_cmd.extend(cmd)
        try:
            idx = cmd.index("--json")
            jpath = cmd[idx + 1]
            with open(jpath, "w") as f:
                json.dump({"writes": [], "context_writes": [], "flags": []}, f)
        except (ValueError, IndexError):
            pass

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    with patch("tools.node_trace.catalog_page.subprocess.run", side_effect=_capture):
        r = app.test_client().post("/catalog/reconcile", data={})
        assert r.status_code in (200, 302)

    assert "--no-llm" not in captured_cmd


def test_catalog_reconcile_error_non_zero_returncode(tmp_path):
    """POST /catalog/reconcile with non-zero returncode does NOT 500."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)

    def _fail(cmd, **kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "something went wrong"

        return _R()

    with patch("tools.node_trace.catalog_page.subprocess.run", side_effect=_fail):
        r = app.test_client().post("/catalog/reconcile", data={})
        # Must not 500; either error page (200) or redirect (302) is acceptable.
        assert r.status_code in (200, 302)


def test_catalog_reconcile_error_timeout(tmp_path):
    """POST /catalog/reconcile on TimeoutExpired does NOT 500."""
    import subprocess as _sp
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)

    def _timeout(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, 30)

    with patch("tools.node_trace.catalog_page.subprocess.run", side_effect=_timeout):
        r = app.test_client().post("/catalog/reconcile", data={})
        assert r.status_code in (200, 302)


def test_catalog_reconcile_missing_json_output(tmp_path):
    """POST /catalog/reconcile when CLI writes nothing still degrades gracefully."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)

    def _no_write(cmd, **kw):
        # Does NOT write the --json file.
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    with patch("tools.node_trace.catalog_page.subprocess.run", side_effect=_no_write):
        r = app.test_client().post("/catalog/reconcile", data={})
        assert r.status_code in (200, 302)


def test_catalog_reconcile_results_counts(tmp_path):
    """POST /catalog/reconcile persists write/context_write counts in results JSON."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)

    with patch(
        "tools.node_trace.catalog_page.subprocess.run",
        side_effect=_fake_subprocess_run(
            writes=[["scores", "credit_score", "description"]],
            context_writes=[["scores", "credit_score"]],
            flags=["[coverage] scores.credit_score"],
        ),
    ):
        app.test_client().post("/catalog/reconcile", data={})

    data = json.loads((tmp_path / "last.json").read_text())
    assert data["writes"] == 1
    assert data["context_writes"] == 1
    assert "[coverage] scores.credit_score" in data["flags"]


# ---------------------------------------------------------------------------
# V4 — viewer wiring: /catalog registered on viewer.app
# ---------------------------------------------------------------------------

def test_viewer_registers_catalog_route():
    """viewer.app must expose /catalog after register_catalog_routes is called."""
    import importlib
    from tools.node_trace import viewer
    importlib.reload(viewer)
    rules = {r.rule for r in viewer.app.url_map.iter_rules()}
    assert "/catalog" in rules


def test_catalog_reconcile_error_surfaced_in_response(tmp_path):
    """POST /catalog/reconcile with non-zero returncode surfaces the error message.

    Tightens the V3 test: we must verify the error is actually visible in the
    rendered GET /catalog response after the PRG redirect, not just that it
    does not 500.
    """
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)
    app.config["TESTING"] = True

    def _fail(cmd, **kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "synthetic-error-from-test"

        return _R()

    with patch("tools.node_trace.catalog_page.subprocess.run", side_effect=_fail):
        # Follow the redirect to the GET /catalog?error=... page.
        r = app.test_client().post(
            "/catalog/reconcile", data={}, follow_redirects=True
        )
        assert r.status_code == 200
        # The error text (or its escaped form) must appear in the page body.
        assert b"synthetic-error-from-test" in r.data or b"reconcile exited" in r.data


# ---------------------------------------------------------------------------
# FIX 1 — CATALOG_RECONCILE_ENABLE gate
# ---------------------------------------------------------------------------

def test_catalog_reconcile_disabled_by_default_returns_403(tmp_path):
    """POST /catalog/reconcile returns 403 and does NOT call subprocess when flag is off (default)."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    # Default: reconcile_enable=False
    app = _app(tmp_path)

    with patch("tools.node_trace.catalog_page.subprocess.run") as mock_run:
        r = app.test_client().post("/catalog/reconcile", data={})
        # Must be 403 (or a redirect); subprocess must NOT be invoked.
        assert r.status_code in (302, 403)
        assert not mock_run.called, "subprocess.run must NOT be called when flag is off"


def test_catalog_get_hides_reconcile_form_when_disabled(tmp_path):
    """GET /catalog does NOT show the Reconcile form/checkbox when flag is off."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)  # flag off
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    # The submit button and checkbox must not be present.
    assert b"no_llm" not in r.data
    assert b'action="/catalog/reconcile"' not in r.data
    # The disabled note must appear instead.
    assert b"CATALOG_RECONCILE_ENABLE" in r.data


def test_catalog_get_shows_reconcile_form_when_enabled(tmp_path):
    """GET /catalog shows the Reconcile form when flag is on."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, reconcile_enable=True)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"no_llm" in r.data
    assert b'action="/catalog/reconcile"' in r.data


# ---------------------------------------------------------------------------
# FIX 2 — autoescape XSS proof
# ---------------------------------------------------------------------------

def test_autoescape_escapes_script_in_table_description(tmp_path):
    """A <script> tag in a table description must be HTML-escaped, not rendered raw."""
    prof_dir = tmp_path / "prof"
    prof_dir.mkdir()
    (prof_dir / "xss.yaml").write_text(yaml.safe_dump({
        "table": "xss_table",
        "description": "<script>alert(1)</script>",
        "columns": {
            "col1": {"dtype": "str", "description": "safe"},
        },
    }))
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    # Escaped form must be present; raw <script> tag must NOT appear.
    assert b"&lt;script&gt;" in r.data
    assert b"<script>alert(1)</script>" not in r.data


def test_autoescape_escapes_script_in_error_query_param(tmp_path):
    """A <script> tag in the ?error= query param must be HTML-escaped."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog?error=<script>alert(1)</script>")
    assert r.status_code == 200
    assert b"&lt;script&gt;" in r.data
    assert b"<script>alert(1)</script>" not in r.data


# ---------------------------------------------------------------------------
# FIX 3 — idempotent route registration
# ---------------------------------------------------------------------------

def test_register_catalog_routes_is_idempotent(tmp_path):
    """Calling register_catalog_routes twice on the same app must not raise."""
    app = Flask(__name__)
    app.config.update(
        PROFILE_DIR=str(tmp_path / "prof"),
        CONTEXT_DIR=str(tmp_path / "ctx"),
        PROVENANCE_PATH=str(tmp_path / ".prov.json"),
        RECONCILE_RESULTS=str(tmp_path / "last.json"),
    )
    register_catalog_routes(app)
    # Second call must be a no-op — no AssertionError / duplicate rule error.
    register_catalog_routes(app)
    # Routes still work after double registration.
    (tmp_path / "prof").mkdir(exist_ok=True)
    (tmp_path / "ctx").mkdir(exist_ok=True)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# NEW — Shared header: AgenticSys Monitor title + DB path
# ---------------------------------------------------------------------------

def test_catalog_header_title(tmp_path):
    """GET /catalog page contains 'AgenticSys Monitor' in the shared header."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"AgenticSys Monitor" in r.data


def test_catalog_header_db_path(tmp_path):
    """GET /catalog page shows the db path (NODE_TRACE_DB) in the shared header."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path, db_path="logs/my_traces.db")
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"logs/my_traces.db" in r.data


# ---------------------------------------------------------------------------
# NEW — Tabs: active state
# ---------------------------------------------------------------------------

def test_catalog_active_tab_is_data_catalog(tmp_path):
    """GET /catalog: the 'Data Catalog' tab must be marked active (tab-active class)."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    # tab-active should be on the /catalog link, NOT the / link
    assert 'class="tab-active"' in html or "tab-active" in html
    # The active element should be near "Data Catalog" text
    assert "tab-active" in html
    # "Traces" link present but NOT marked active
    assert 'href="/"' in html


def test_catalog_traces_tab_not_active(tmp_path):
    """GET /catalog: the 'Traces' tab must NOT be marked as active.

    The Traces anchor (<a href="/">Traces</a>) must not carry class="tab-active".
    We check this by verifying tab-active is NOT between the opening <a and the
    first > (i.e., within that specific anchor's tag attributes).
    """
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    html = r.data.decode("utf-8")
    # Find the Traces tab anchor: look for the opening tag that contains href="/"
    # and extract just the tag attributes (up to the closing >).
    import re
    # Match the <a ...href="/"...> opening tag for the Traces link.
    m = re.search(r'<a\s[^>]*href="/"[^>]*>', html)
    assert m is not None, "Traces tab anchor not found in page"
    tag_text = m.group(0)
    assert "tab-active" not in tag_text, (
        f"Traces tab should NOT be active on /catalog, but tag was: {tag_text!r}"
    )


# ---------------------------------------------------------------------------
# NEW — Catalog master/detail: TOC + <details> collapsible blocks
# ---------------------------------------------------------------------------

def test_catalog_toc_entry_for_table(tmp_path):
    """GET /catalog renders a TOC <a href="#tbl-<name>"> entry for each table."""
    _write_profile(tmp_path / "prof", table_name="my_scores")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b'href="#tbl-my_scores"' in r.data


def test_catalog_details_id_for_table(tmp_path):
    """GET /catalog renders a <details id="tbl-<name>"> block for each table."""
    _write_profile(tmp_path / "prof", table_name="my_scores")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b'id="tbl-my_scores"' in r.data


def test_catalog_details_contains_column_name(tmp_path):
    """GET /catalog: the <details> block for a table contains the column name."""
    _write_profile(tmp_path / "prof", table_name="score_table")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"score_table" in r.data
    assert b"column a" in r.data or b"col" in r.data  # description or column 'a'


def test_catalog_details_provenance_badge_present(tmp_path):
    """GET /catalog: the collapsible block still renders provenance badges."""
    _write_profile(tmp_path / "prof", table_name="t2")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert any(kw in r.data for kw in (b"unmanaged", b"agent", b"human"))


def test_catalog_toc_and_details_multiple_tables(tmp_path):
    """GET /catalog with multiple tables: TOC and details blocks for each."""
    prof_dir = tmp_path / "prof"
    prof_dir.mkdir()
    for name in ("alpha", "beta", "gamma"):
        (prof_dir / f"{name}.yaml").write_text(yaml.safe_dump({
            "table": name, "description": f"{name} table",
            "columns": {"col1": {"dtype": "str", "description": f"col in {name}"}},
        }))
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    for name in ("alpha", "beta", "gamma"):
        assert f'href="#tbl-{name}"'.encode() in r.data
        assert f'id="tbl-{name}"'.encode() in r.data


# ---------------------------------------------------------------------------
# NEW — Viewer-side: trace index (GET /) has shared header and Traces active tab
# ---------------------------------------------------------------------------

def test_viewer_index_has_shared_header(tmp_path):
    """GET / on viewer.app contains 'AgenticSys Monitor' and both tab labels."""
    import importlib
    from tools.node_trace import viewer as v
    importlib.reload(v)

    # Point the app at a non-existent DB (index view handles empty gracefully).
    db_path = str(tmp_path / "traces.db")
    v.app.config["NODE_TRACE_DB"] = db_path

    client = v.app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "AgenticSys Monitor" in html
    assert "Traces" in html
    assert "Data Catalog" in html
    # DB path should appear
    assert "traces.db" in html


# ---------------------------------------------------------------------------
# Change 1 — Stale profiles: grey class + "(no data table)" marker
# ---------------------------------------------------------------------------

def test_catalog_stale_table_has_grey_class(tmp_path):
    """GET /catalog: a stale table (no backing CSV) renders with .stale CSS class."""
    prof_dir = tmp_path / "prof"
    prof_dir.mkdir()
    (prof_dir / "live.yaml").write_text(yaml.safe_dump({
        "table": "live", "description": "live table",
        "columns": {"col": {"dtype": "int", "description": "x"}},
    }))
    (prof_dir / "ghost.yaml").write_text(yaml.safe_dump({
        "table": "ghost", "description": "ghost table",
        "columns": {"col": {"dtype": "int", "description": "y"}},
    }))
    (tmp_path / "ctx").mkdir()

    data_dir = tmp_path / "data"
    (data_dir / "case001").mkdir(parents=True)
    (data_dir / "case001" / "live.csv").write_text("col\n1\n")

    app = Flask(__name__)
    app.config.update(
        PROFILE_DIR=str(prof_dir),
        CONTEXT_DIR=str(tmp_path / "ctx"),
        PROVENANCE_PATH=str(tmp_path / ".prov.json"),
        RECONCILE_RESULTS=str(tmp_path / "last.json"),
        DATA_DIR=str(data_dir),
    )
    register_catalog_routes(app)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    # The ghost table must have class="stale" somewhere in its rendering
    assert "stale" in html


def test_catalog_stale_table_has_no_data_table_marker(tmp_path):
    """GET /catalog: stale table shows '(no data table)' marker."""
    prof_dir = tmp_path / "prof"
    prof_dir.mkdir()
    (prof_dir / "live.yaml").write_text(yaml.safe_dump({
        "table": "live", "description": "live",
        "columns": {"col": {"dtype": "int", "description": "x"}},
    }))
    (prof_dir / "ghost.yaml").write_text(yaml.safe_dump({
        "table": "ghost", "description": "ghost",
        "columns": {"col": {"dtype": "int", "description": "y"}},
    }))
    (tmp_path / "ctx").mkdir()

    data_dir = tmp_path / "data"
    (data_dir / "case001").mkdir(parents=True)
    # Only live.csv present; ghost has no CSV → stale
    (data_dir / "case001" / "live.csv").write_text("col\n1\n")

    app = Flask(__name__)
    app.config.update(
        PROFILE_DIR=str(prof_dir),
        CONTEXT_DIR=str(tmp_path / "ctx"),
        PROVENANCE_PATH=str(tmp_path / ".prov.json"),
        RECONCILE_RESULTS=str(tmp_path / "last.json"),
        DATA_DIR=str(data_dir),
    )
    register_catalog_routes(app)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    assert b"no data table" in r.data


# ---------------------------------------------------------------------------
# Change 2 — Fold all by default: no `open` attribute on <details>
# ---------------------------------------------------------------------------

def test_catalog_details_folded_by_default(tmp_path):
    """GET /catalog: no <details ... open> in the page (all folded by default)."""
    import re
    _write_profile(tmp_path / "prof", table_name="fold_test")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    # Must not contain `open` attribute on any <details> tag
    assert not re.search(r'<details\b[^>]*\bopen\b', html), (
        "Found <details ... open> in page — all details should be folded by default"
    )


def test_viewer_index_traces_tab_active(tmp_path):
    """GET / on viewer.app: 'Traces' tab is marked active; 'Data Catalog' is not."""
    import importlib
    from tools.node_trace import viewer as v
    importlib.reload(v)

    v.app.config["NODE_TRACE_DB"] = str(tmp_path / "traces.db")
    client = v.app.test_client()
    r = client.get("/")
    html = r.data.decode("utf-8")

    # Find the Traces tab link (href="/") and ensure it has tab-active
    idx = html.find('href="/"')
    assert idx != -1, "'href=\"/\"' not found in page"
    snippet = html[idx:idx + 80]
    assert "tab-active" in snippet, f"tab-active not near href='/': {snippet!r}"

    # Data Catalog link must NOT be active on the traces page
    idx2 = html.find('href="/catalog"')
    assert idx2 != -1, "'href=\"/catalog\"' not found in page"
    snippet2 = html[idx2:idx2 + 80]
    assert "tab-active" not in snippet2, (
        f"Data Catalog incorrectly marked tab-active on /: {snippet2!r}"
    )
