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
    "DATA_DIR": "data_tables/real",
}

_RECONCILE_TIMEOUT = 120  # seconds

# ── Style (mirrors viewer.py's inline CSS) ────────────────────────────────────

_STYLE = """
<style>
html, body { margin: 0; padding: 0; }
body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       color: #1a1a1a; box-sizing: border-box; }
/* Shared header */
.site-header { background: #1e3a5f; color: #fff; padding: 12px 24px 0; }
.site-header .site-title { font-size: 18px; font-weight: 700; letter-spacing: -0.2px;
                            color: #fff; text-decoration: none; }
.site-header .site-db { font-size: 11px; color: #93c5fd; margin-top: 2px;
                         font-family: SF Mono, Menlo, Consolas, monospace; }
/* Tab nav */
.tab-nav { display: flex; gap: 0; margin-top: 10px; }
.tab-nav a { display: inline-block; padding: 8px 18px; font-size: 13px; font-weight: 500;
             color: #93c5fd; text-decoration: none; border-bottom: 3px solid transparent;
             border-radius: 4px 4px 0 0; transition: color 0.1s; }
.tab-nav a:hover { color: #fff; background: rgba(255,255,255,0.07); }
.tab-nav a.tab-active { color: #fff; border-bottom-color: #60a5fa; font-weight: 700; }
/* Page body */
.page-body { padding: 24px; }
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
/* Stale (no backing data table) */
.stale { color: #9ca3af; }
.stale summary { color: #9ca3af; }
.stale-marker { font-size: 11px; font-weight: normal; color: #9ca3af;
                margin-left: 6px; font-style: italic; }
/* Legend */
.legend { font-size: 12px; color: #6b7280; margin-bottom: 16px; }
.legend .badge { margin-right: 6px; }
/* Catalog master/detail layout */
.catalog-layout { display: flex; gap: 24px; align-items: flex-start; }
/* TOC — Amex font stack (Benton Sans degrades to Helvetica Neue / Helvetica / Arial) */
.catalog-toc { flex: 0 0 200px; min-width: 160px; position: sticky; top: 16px; }
.catalog-toc ul { list-style: none; margin: 0; padding: 0; }
.catalog-toc li { margin: 0; }
.catalog-toc a { display: block; padding: 5px 10px; font-size: 13px; font-weight: 500;
                 color: #4A5568; border-radius: 2px;
                 font-family: "Benton Sans", "Helvetica Neue", Helvetica, Arial, sans-serif;
                 letter-spacing: 0.02em; }
.catalog-toc a:hover { background: #F4F6F9; color: #006FCF; text-decoration: none; }
.catalog-toc-title { font-size: 11px; font-weight: 700; text-transform: uppercase;
                      letter-spacing: 0.08em; color: #4A5568; margin-bottom: 8px;
                      font-family: "Benton Sans", "Helvetica Neue", Helvetica, Arial, sans-serif; }
.catalog-main { flex: 1 1 0; min-width: 0; }
/* Group separator in the main panel */
.catalog-group { margin-bottom: 28px; }
/* Collapsible group details */
details.grp-block { border: 1px solid #e5e7eb; border-radius: 6px;
                    margin-bottom: 20px; overflow: hidden; }
details.grp-block > summary { background: #f9fafb; padding: 10px 14px;
                               border-bottom: 1px solid #e5e7eb; cursor: pointer;
                               font-size: 15px; font-weight: 600; list-style: none; }
details.grp-block > summary::-webkit-details-marker { display: none; }
details.grp-block > summary::before { content: "▶ "; font-size: 10px;
                                       color: #6b7280; margin-right: 6px; }
details.grp-block[open] > summary::before { content: "▼ "; }
details.grp-block > summary .tbl-desc { font-weight: normal; font-size: 13px;
                                         color: #6b7280; margin-left: 6px; }
details.grp-block > summary .tbl-aliases { font-weight: normal; font-size: 12px;
                                            color: #9ca3af; }
/* Monthly/Transaction tabs */
.tab-bar { display: flex; gap: 0; border-bottom: 2px solid #E2E8F0; margin: 0; }
.tab-btn { padding: 10px 22px; font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
           font-size: 12px; font-weight: 600; text-transform: uppercase;
           letter-spacing: 0.08em; color: #4A5568;
           border: none; border-bottom: 3px solid transparent; background: none;
           cursor: pointer; margin-bottom: -2px; transition: color 0.15s; }
.tab-btn.tab-active { color: #006FCF; border-bottom-color: #006FCF; }
.tab-btn:hover { color: #00175A; }
.tab-panel { display: none; }
.tab-panel.tab-visible { display: block; }
.tbl-inner { padding: 0; }
</style>
"""

# ── HTML template ─────────────────────────────────────────────────────────────

_CATALOG_TMPL = """{% autoescape true %}<!doctype html>
<html><head><meta charset="utf-8"><title>Data Catalog · AgenticSys Monitor</title>""" + _STYLE + """</head>
<body>
  <!-- Shared header -->
  <header class="site-header">
    <div class="site-title">AgenticSys Monitor</div>
    <div class="site-db">db: {{ db_path }}</div>
    <nav class="tab-nav">
      <a href="/">Traces</a>
      <a href="/catalog" class="tab-active">Data Catalog</a>
    </nav>
  </header>

  <div class="page-body">

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

  <!-- Reconcile form (shown only when CATALOG_RECONCILE_ENABLE is on) -->
  {% if reconcile_enabled %}
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
  {% else %}
  <div class="reconcile-box">
    <p class="muted" style="margin:0;">
      Reconcile trigger disabled &mdash; set <code>CATALOG_RECONCILE_ENABLE=1</code> to enable.
    </p>
  </div>
  {% endif %}

  <!-- Provenance legend -->
  <div class="legend">
    Provenance:
    <span class="badge badge-agent">agent</span> agent-managed (baseline = current) ·
    <span class="badge badge-human">human</span> human-edited (baseline differs) ·
    <span class="badge badge-unmanaged">unmanaged</span> no baseline recorded
  </div>

  <!-- Master / detail: TOC + collapsible group blocks -->
  {% if not tables %}
    <p class="muted">No data-profile YAML files found in <code>{{ profile_dir }}</code>.</p>
  {% else %}
  <div class="catalog-layout">
    <!-- Left: table-of-contents — one entry per domain group -->
    <aside class="catalog-toc">
      <div class="catalog-toc-title">Tables</div>
      <ul>
        {% for grp in groups %}
        <li><a href="#grp-{{ grp.key }}">{{ grp.key }}</a></li>
        {% endfor %}
      </ul>
    </aside>

    <!-- Right: one collapsible <details> per group; tabs inside for multi-member groups -->
    <div class="catalog-main">
      {% for grp in groups %}
      <div class="catalog-group">
        <details class="grp-block" id="grp-{{ grp.key }}">
          <summary>
            {{ grp.key }}
            {% if grp.tables | length == 1 %}
              {% set tbl = grp.tables[0] %}
              {% if tbl.description_short %}
                <span class="tbl-desc" title="{{ tbl.description }}">— {{ tbl.description_short }}</span>
              {% endif %}
              {% if tbl.aliases %}
                <span class="tbl-aliases">(aliases: {{ tbl.aliases | join(", ") }})</span>
              {% endif %}
            {% else %}
              {% set base_tbl = grp.tables[0] %}
              {% if base_tbl.description_short %}
                <span class="tbl-desc" title="{{ base_tbl.description }}">— {{ base_tbl.description_short }}</span>
              {% endif %}
            {% endif %}
          </summary>
          {% if grp.tables | length > 1 %}
          <!-- Horizontal tabs: Monthly (base) + Transaction -->
          <div class="tab-bar" id="tabs-{{ grp.key }}">
            <button class="tab-btn tab-active"
                    onclick="switchTab('{{ grp.key }}', 0, this)">Monthly</button>
            <button class="tab-btn"
                    onclick="switchTab('{{ grp.key }}', 1, this)">Transaction</button>
          </div>
          {% for tbl in grp.tables %}
          <div class="tab-panel{% if loop.index == 1 %} tab-visible{% endif %}"
               id="tab-{{ grp.key }}-{{ loop.index0 }}"
               data-group="{{ grp.key }}" data-idx="{{ loop.index0 }}">
            <div class="tbl-inner" id="tbl-{{ tbl.table }}">
              {% if tbl.description %}
              <p style="font-size:13px;color:#4A5568;padding:8px 14px 0;margin:0;"
                 title="{{ tbl.description }}">{{ tbl.description_short }}</p>
              {% endif %}
              {% if tbl.aliases %}
              <p style="font-size:12px;color:#9ca3af;padding:2px 14px 0;margin:0;">
                aliases: {{ tbl.aliases | join(", ") }}</p>
              {% endif %}
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
            </div>
          </div>
          {% endfor %}
          {% else %}
          <!-- Single table: no tabs needed -->
          {% set tbl = grp.tables[0] %}
          <div class="tbl-inner" id="tbl-{{ tbl.table }}">
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
          </div>
          {% endif %}
        </details>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  </div><!-- /page-body -->

  <script>
  /* Tab switching for Monthly/Transaction groups */
  function switchTab(groupKey, idx, btn) {
    // Deactivate all tab buttons in this group
    var bar = document.getElementById('tabs-' + groupKey);
    if (bar) {
      bar.querySelectorAll('.tab-btn').forEach(function(b){ b.classList.remove('tab-active'); });
    }
    btn.classList.add('tab-active');
    // Hide all panels in this group, show the selected one
    document.querySelectorAll('.tab-panel[data-group="' + groupKey + '"]').forEach(function(p){
      p.classList.remove('tab-visible');
    });
    var active = document.getElementById('tab-' + groupKey + '-' + idx);
    if (active) active.classList.add('tab-visible');
  }

  /* TOC anchor + open: clicking a TOC link opens the group <details> and scrolls to it */
  (function(){
    function openTarget(hash){
      if (!hash) return;
      var id = hash.replace(/^#/, '');
      var el = document.getElementById(id);
      if (el && el.tagName === 'DETAILS') {
        el.open = true;
        el.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    }
    document.querySelectorAll('.catalog-toc a').forEach(function(a){
      a.addEventListener('click', function(e){
        setTimeout(function(){ openTarget(a.getAttribute('href')); }, 0);
      });
    });
    window.addEventListener('hashchange', function(){
      openTarget(window.location.hash);
    });
    if (window.location.hash) openTarget(window.location.hash);
  })();
  </script>
</body></html>
{% endautoescape %}
"""


# ── Route registration ────────────────────────────────────────────────────────

def register_catalog_routes(app) -> None:
    """Attach GET /catalog and POST /catalog/reconcile to *app*.

    Safe to call more than once on the same app (single registration per app).
    """
    # Single registration per app — guard against double-import / reload loops.
    if "catalog_get" in app.view_functions:
        return

    # Default OFF unless CATALOG_RECONCILE_ENABLE=1|true|True is set.
    app.config.setdefault(
        "CATALOG_RECONCILE_ENABLE",
        os.environ.get("CATALOG_RECONCILE_ENABLE") in ("1", "true", "True"),
    )

    def _cfg(key: str) -> str:
        return app.config.get(key) or _DEFAULTS[key]

    @app.get("/catalog")
    def catalog_get():
        profile_dir = _cfg("PROFILE_DIR")
        context_dir = _cfg("CONTEXT_DIR")
        provenance_path = _cfg("PROVENANCE_PATH")
        reconcile_results = _cfg("RECONCILE_RESULTS")
        data_dir = _cfg("DATA_DIR")

        # Gracefully handle missing dirs (e.g. first run before profiles created).
        try:
            view = build_catalog_view(profile_dir, context_dir, provenance_path, data_dir=data_dir)
            tables = view.get("tables", [])
            groups = view.get("groups", [])
        except Exception as exc:
            tables = []
            groups = []
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

        # Resolve the DB path from app.config (same key viewer.py uses).
        db_path = app.config.get("NODE_TRACE_DB") or _DEFAULTS.get(
            "NODE_TRACE_DB", "logs/node_traces.db"
        )

        return render_template_string(
            _CATALOG_TMPL,
            tables=tables,
            groups=groups,
            last_run=last_run,
            error=error,
            profile_dir=profile_dir,
            reconcile_enabled=app.config.get("CATALOG_RECONCILE_ENABLE", False),
            db_path=str(db_path),
        )

    @app.post("/catalog/reconcile")
    def catalog_reconcile():
        # Gate: reconcile trigger is opt-in (default OFF).
        if not app.config.get("CATALOG_RECONCILE_ENABLE", False):
            return (
                "Reconcile trigger is disabled. "
                "Set CATALOG_RECONCILE_ENABLE=1 to enable.",
                403,
            )

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
