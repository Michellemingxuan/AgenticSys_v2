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
# Helpers
# ---------------------------------------------------------------------------

def _app(tmp_path, reconcile_enable: bool = False):
    """Build a test Flask app.  Pass reconcile_enable=True for tests that
    exercise the enabled path; the default mirrors the production default (OFF).
    """
    app = Flask(__name__)
    app.config.update(
        PROFILE_DIR=str(tmp_path / "prof"),
        CONTEXT_DIR=str(tmp_path / "ctx"),
        PROVENANCE_PATH=str(tmp_path / ".prov.json"),
        RECONCILE_RESULTS=str(tmp_path / "last.json"),
        CATALOG_RECONCILE_ENABLE=reconcile_enable,
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
    """GET /catalog includes nav links to Traces (/) and Catalog."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
    r = app.test_client().get("/catalog")
    assert r.status_code == 200
    # Both Traces and Catalog nav targets present.
    assert b'href="/"' in r.data
    assert b"/catalog" in r.data


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
