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

def _app(tmp_path):
    app = Flask(__name__)
    app.config.update(
        PROFILE_DIR=str(tmp_path / "prof"),
        CONTEXT_DIR=str(tmp_path / "ctx"),
        PROVENANCE_PATH=str(tmp_path / ".prov.json"),
        RECONCILE_RESULTS=str(tmp_path / "last.json"),
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
    """GET /catalog includes the Reconcile form with no_llm checkbox."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)
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
    app = _app(tmp_path)

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
    app = _app(tmp_path)
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
    app = _app(tmp_path)
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
    app = _app(tmp_path)

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
    app = _app(tmp_path)

    def _timeout(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, 30)

    with patch("tools.node_trace.catalog_page.subprocess.run", side_effect=_timeout):
        r = app.test_client().post("/catalog/reconcile", data={})
        assert r.status_code in (200, 302)


def test_catalog_reconcile_missing_json_output(tmp_path):
    """POST /catalog/reconcile when CLI writes nothing still degrades gracefully."""
    _write_profile(tmp_path / "prof")
    (tmp_path / "ctx").mkdir()
    app = _app(tmp_path)

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
    app = _app(tmp_path)

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
