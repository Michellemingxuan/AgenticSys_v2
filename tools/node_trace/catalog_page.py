"""Flask routes for the /catalog page.

Registers two routes on an existing Flask app:
    GET  /catalog           — renders build_catalog_view as an HTML dashboard.
    POST /catalog/reconcile — shells out to ``python -m datalayer.sync
                              --reconcile --json <tmp>``, reads the JSON
                              result, persists a small summary, then redirects
                              to /catalog (Post/Redirect/Get).

All data-profile / context / provenance / results paths are taken from
``app.config`` (with defaults).  Import ``subprocess`` at module level so
tests can patch ``tools.node_trace.catalog_page.subprocess.run``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from flask import redirect, render_template_string, request, url_for

from tools.node_trace.catalog_view import build_catalog_view

# ── Config-key defaults ───────────────────────────────────────────────────────

_DEFAULTS: dict[str, str] = {
    "PROFILE_DIR": "config/data_profiles",
    "CONTEXT_DIR": "context",
    "PROVENANCE_PATH": "config/data_profiles/.provenance.json",
    "RECONCILE_RESULTS": "logs/last_reconcile.json",
}

_RECONCILE_TIMEOUT = 120  # seconds

# ── Style (mirrors viewer.py's inline CSS) ────────────────────────────────────

_STYLE = """
<style>
html, body { margin: 0; padding: 0; }
body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       padding: 24px; color: #1a1a1a; box-sizing: border-box; }
h1 { font-size: 20px; margin: 0 0 16px; }
h2 { font-size: 16px; margin: 24px 0 8px; }
h3 { font-size: 14px; margin: 16px 0 6px; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
th, td { padding: 6px 10px; border-bottom: 1px solid #e5e7eb;
         font-variant-numeric: tabular-nums; }
td { text-align: left; }
th { background: #f9fafb; font-weight: 600; text-align: left; }
tr:hover td { background: #fafbfc; }
.muted { color: #6b7280; }
nav { margin-bottom: 20px; font-size: 13px; border-bottom: 1px solid #e5e7eb;
      padding-bottom: 10px; }
nav a { margin-right: 14px; }
nav a.active { font-weight: 700; color: #1a1a1a; text-decoration: none; }
.card { border: 1px solid #e5e7eb; border-radius: 6px;
        margin-bottom: 24px; overflow: hidden; }
.card-header { background: #f9fafb; padding: 10px 14px;
               border-bottom: 1px solid #e5e7eb; }
.card-header h2 { margin: 0; }
.card-body { padding: 0; }
/* Provenance badges */
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
         font-size: 11px; font-weight: 600; text-transform: uppercase;
         letter-spacing: 0.4px; }
.badge-agent    { background: #d1fae5; color: #065f46; }
.badge-human    { background: #dbeafe; color: #1e40af; }
.badge-unmanaged { background: #f3f4f6; color: #6b7280; }
/* Context check/cross */
.ctx-yes { color: #16a34a; font-weight: 700; }
.ctx-no  { color: #9ca3af; }
/* Threshold */
.threshold { font-family: SF Mono, Menlo, Consolas, monospace; font-size: 12px; }
/* Reconcile form */
.reconcile-box { border: 1px solid #e5e7eb; border-radius: 6px; padding: 16px 20px;
                 background: #fafafa; margin-bottom: 24px; }
.reconcile-box h2 { margin: 0 0 10px; }
.reconcile-box label { font-size: 13px; color: #374151; }
.btn { display: inline-block; padding: 7px 18px; border-radius: 4px; font-size: 13px;
       font-weight: 600; cursor: pointer; border: 1px solid transparent; }
.btn-primary { background: #2563eb; color: #fff; border-color: #2563eb; }
.btn-primary:hover { background: #1d4ed8; }
/* Last run panel */
.run-panel { border: 1px solid #e5e7eb; border-radius: 6px; padding: 14px 18px;
             background: #fff; margin-bottom: 24px; }
.run-panel h2 { margin: 0 0 8px; }
.flag-list { list-style: none; margin: 6px 0 0; padding: 0; }
.flag-list li { padding: 2px 0; font-family: SF Mono, Menlo, Consolas, monospace;
                font-size: 12px; color: #374151; }
/* Error panel */
.error-panel { border: 1px solid #fca5a5; border-radius: 6px; padding: 14px 18px;
               background: #fef2f2; margin-bottom: 24px; color: #991b1b; }
/* Context-only list */
.ctx-only { font-family: SF Mono, Menlo, Consolas, monospace; font-size: 12px;
             color: #6b7280; padding: 4px 14px 10px; }
.ctx-only span { margin-right: 8px; }
/* Legend */
.legend { font-size: 12px; color: #6b7280; margin-bottom: 16px; }
.legend .badge { margin-right: 6px; }
</style>
"""

# ── HTML template ─────────────────────────────────────────────────────────────

_CATALOG_TMPL = """{% autoescape true %}<!doctype html>
<html><head><meta charset="utf-8"><title>Catalog · node_trace</title>""" + _STYLE + """</head>
<body>
  <h1>Data Catalog</h1>
  <nav>
    <a href="/">Traces</a>
    <a href="/catalog" class="active">Catalog</a>
  </nav>

  {% if error %}
  <div class="error-panel">
    <strong>Reconcile error:</strong> {{ error }}
  </div>
  {% endif %}

  <!-- Last-run panel -->
  {% if last_run %}
  <div class="run-panel">
    <h2>Last reconcile — {{ last_run.timestamp }}</h2>
    <p style="margin:0 0 6px;">
      <strong>{{ last_run.writes }}</strong> profile write(s) ·
      <strong>{{ last_run.context_writes }}</strong> context write(s)
      {% if last_run.no_llm %}<span class="muted"> · no-LLM mode</span>{% endif %}
    </p>
    {% if last_run.flags %}
    <ul class="flag-list">
      {% for f in last_run.flags %}<li>{{ f }}</li>{% endfor %}
    </ul>
    {% else %}
    <p class="muted" style="margin:0;">No flags.</p>
    {% endif %}
  </div>
  {% endif %}

  <!-- Reconcile form -->
  <div class="reconcile-box">
    <h2>Reconcile</h2>
    <form method="POST" action="/catalog/reconcile">
      <label>
        <input type="checkbox" name="no_llm"> Run without LLM (--no-llm)
      </label>
      &nbsp;&nbsp;
      <button type="submit" class="btn btn-primary">Run reconcile</button>
    </form>
  </div>

  <!-- Provenance legend -->
  <div class="legend">
    Provenance:
    <span class="badge badge-agent">agent</span> agent-managed (baseline = current) ·
    <span class="badge badge-human">human</span> human-edited (baseline differs) ·
    <span class="badge badge-unmanaged">unmanaged</span> no baseline recorded
  </div>

  <!-- Table cards -->
  {% if not tables %}
    <p class="muted">No data-profile YAML files found in <code>{{ profile_dir }}</code>.</p>
  {% endif %}

  {% for tbl in tables %}
  <div class="card">
    <div class="card-header">
      <h2>{{ tbl.table }}
        {% if tbl.description %}
          <span class="muted" style="font-weight:normal;font-size:13px;">— {{ tbl.description }}</span>
        {% endif %}
        {% if tbl.aliases %}
          <span class="muted" style="font-size:12px;font-weight:normal;">
            (aliases: {{ tbl.aliases | join(", ") }})
          </span>
        {% endif %}
      </h2>
    </div>
    <div class="card-body">
      <table>
        <thead>
          <tr>
            <th>Column</th>
            <th>dtype</th>
            <th>Description</th>
            <th>Threshold</th>
            <th title="Has a context entry">In context</th>
            <th>Provenance</th>
          </tr>
        </thead>
        <tbody>
          {% for col in tbl.columns %}
          <tr>
            <td><strong>{{ col.name }}</strong></td>
            <td class="muted">{{ col.dtype }}{% if col.parse_hint %} <span title="{{ col.parse_hint }}">({{ col.parse_hint }})</span>{% endif %}</td>
            <td>{{ col.description or "" }}</td>
            <td>
              {% if col.threshold %}
                <span class="threshold">{{ col.threshold.value }} ({{ col.threshold.direction }})</span>
              {% else %}
                <span class="muted">—</span>
              {% endif %}
            </td>
            <td>
              {% if col.in_context %}
                <span class="ctx-yes" title="context entry present">&#x2713;</span>
              {% else %}
                <span class="ctx-no" title="not in context">&#x2717;</span>
              {% endif %}
            </td>
            <td>
              <span class="badge badge-{{ col.provenance }}">{{ col.provenance }}</span>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% if tbl.context_only %}
      <div class="ctx-only">
        <strong>Context-only vars (not in profile):</strong>
        {% for v in tbl.context_only %}<span>{{ v }}</span>{% endfor %}
      </div>
      {% endif %}
    </div>
  </div>
  {% endfor %}
</body></html>
{% endautoescape %}
"""


# ── Route registration ────────────────────────────────────────────────────────

def register_catalog_routes(app) -> None:
    """Attach GET /catalog and POST /catalog/reconcile to *app*."""

    def _cfg(key: str) -> str:
        return app.config.get(key) or _DEFAULTS[key]

    @app.get("/catalog")
    def catalog_get():
        profile_dir = _cfg("PROFILE_DIR")
        context_dir = _cfg("CONTEXT_DIR")
        provenance_path = _cfg("PROVENANCE_PATH")
        reconcile_results = _cfg("RECONCILE_RESULTS")

        # Gracefully handle missing dirs (e.g. first run before profiles created).
        try:
            view = build_catalog_view(profile_dir, context_dir, provenance_path)
            tables = view.get("tables", [])
        except Exception as exc:
            tables = []
            # Surface as a warning but don't 500.
            app.logger.warning("build_catalog_view failed: %s", exc)

        # Load last-reconcile summary if it exists.
        last_run = None
        if os.path.exists(reconcile_results):
            try:
                with open(reconcile_results, encoding="utf-8") as fh:
                    last_run = json.load(fh)
            except Exception:
                pass

        # Pull flash-style error from query param (set by the reconcile POST).
        error = request.args.get("error") or ""

        return render_template_string(
            _CATALOG_TMPL,
            tables=tables,
            last_run=last_run,
            error=error,
            profile_dir=profile_dir,
        )

    @app.post("/catalog/reconcile")
    def catalog_reconcile():
        profile_dir = _cfg("PROFILE_DIR")
        context_dir = _cfg("CONTEXT_DIR")
        provenance_path = _cfg("PROVENANCE_PATH")
        reconcile_results = _cfg("RECONCILE_RESULTS")

        no_llm = bool(request.form.get("no_llm"))

        # Build command — uses the same Python interpreter that runs the server.
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        ) as tmp:
            tmp_json_path = tmp.name

        cmd = [sys.executable, "-m", "datalayer.sync", "--reconcile",
               "--json", tmp_json_path]
        if no_llm:
            cmd.append("--no-llm")

        error_msg: str = ""
        result_payload: dict | None = None

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_RECONCILE_TIMEOUT,
            )
            if proc.returncode != 0:
                error_msg = (
                    f"reconcile exited with code {proc.returncode}: "
                    f"{proc.stderr.strip()[:400]}"
                )
            else:
                # Try to read the --json output file.
                try:
                    with open(tmp_json_path, encoding="utf-8") as fh:
                        result_payload = json.load(fh)
                except (OSError, json.JSONDecodeError) as exc:
                    error_msg = f"could not read reconcile result: {exc}"

        except subprocess.TimeoutExpired:
            error_msg = f"reconcile timed out after {_RECONCILE_TIMEOUT}s"
        except Exception as exc:
            error_msg = f"reconcile subprocess error: {exc}"
        finally:
            # Clean up temp file (best-effort).
            try:
                os.unlink(tmp_json_path)
            except OSError:
                pass

        # Persist a compact summary (timestamp + counts + flags) to results path.
        if result_payload is not None:
            summary = {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "writes": len(result_payload.get("writes") or []),
                "context_writes": len(result_payload.get("context_writes") or []),
                "flags": result_payload.get("flags") or [],
                "no_llm": no_llm,
            }
            # Ensure parent directory exists.
            try:
                Path(reconcile_results).parent.mkdir(parents=True, exist_ok=True)
                with open(reconcile_results, "w", encoding="utf-8") as fh:
                    json.dump(summary, fh, indent=2)
            except OSError as exc:
                app.logger.warning("could not write reconcile results: %s", exc)

        # Post/Redirect/Get — redirect to /catalog.
        # Pass any error as a query param so it renders in the GET handler.
        if error_msg:
            return redirect(url_for("catalog_get", error=error_msg))
        return redirect(url_for("catalog_get"))
