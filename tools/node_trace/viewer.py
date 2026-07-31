"""Lite Flask viewer for the node_trace SQLite store.

Three views (all self-contained HTML, no JS framework):
    /                          — chat (session) list
    /chat/<chat_id>            — turn list for a chat
    /turn/<chat_id>/<turn_id>  — tree of nodes; click a node to expand
                                 its full structured prompt + completion

Run:
    python -m tools.node_trace.viewer
    python -m tools.node_trace.viewer --port 3002 --db logs/node_traces.db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, Response, abort, redirect, render_template_string,
                   request, url_for)

from tools.node_trace._io import open_db


# Same default as server.py — and honors the same env var so setting
# NODE_TRACE_DB once aligns both the writer (server) and the reader
# (this viewer). Expand ``~`` and ``$VAR`` references so values from
# .env (e.g. ``NODE_TRACE_DB=$HOME/agenticsys-traces/node_traces.db``)
# resolve to a real absolute path.
_DEFAULT_DB = Path(os.path.expanduser(os.path.expandvars(
    os.environ.get("NODE_TRACE_DB", "logs/node_traces.db")
)))
_SGT = timezone(timedelta(hours=8))   # Singapore is UTC+8 (no DST)

# Slowness thresholds for the table-cell color highlight.
_DUR_WARN_S = 10.0
_DUR_BAD_S = 50.0

# Page auto-refresh interval. Pause per request with `?norefresh=1`,
# or hit the "refresh now" link in the top-right badge for an immediate
# reload. 60s is a good default — long enough to read the page without
# constant flicker, short enough that a finished turn shows up promptly.
_REFRESH_SECS = 60

app = Flask(__name__)


def _db() -> Path:
    return Path(app.config.get("NODE_TRACE_DB") or _DEFAULT_DB)


def _iso_to_sgt(s: str | None) -> str:
    """Convert an ISO-8601 UTC timestamp to a compact SGT string."""
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s).astimezone(_SGT).strftime(
            "%Y-%m-%d %H:%M:%S SGT"
        )
    except Exception:
        return s


def _dur_class(seconds: float | None) -> str:
    """CSS class for slowness highlight."""
    if not seconds:
        return ""
    if seconds >= _DUR_BAD_S:
        return "dur-bad"
    if seconds >= _DUR_WARN_S:
        return "dur-warn"
    return ""


def _cache_class(ratio: float | None) -> str:
    """CSS class for cache-hit colorization. None or 0 → no class."""
    if ratio is None:
        return ""
    if ratio < 0.30:
        return "cache-bad"
    if ratio < 0.60:
        return "cache-warn"
    if ratio >= 0.80:
        return "cache-good"
    return ""


def _refresh_secs() -> int:
    """0 when the request asked to pause (?norefresh=1), else _REFRESH_SECS."""
    return 0 if request.args.get("norefresh") == "1" else _REFRESH_SECS


def _question_for_turn(conn, chat_id: str, turn_id: str) -> str:
    """Pull the user's question out of the chat.relevance_check round's
    messages_json (the user message starts with 'Reviewer question: …').
    Returns "" when the row hasn't captured messages_json (older turns).
    """
    row = conn.execute(
        "SELECT messages_json FROM node_trace "
        "WHERE chat_id = ? AND turn_id = ? "
        "  AND node = 'chat.relevance_check.round_1' "
        "  AND messages_json IS NOT NULL "
        "LIMIT 1",
        (chat_id, turn_id),
    ).fetchone()
    if not row or not row["messages_json"]:
        return ""
    try:
        msgs = json.loads(row["messages_json"])
    except Exception:
        return ""
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = str(m.get("content") or "")
        # The relevance_check prompt starts the user block with
        # "Reviewer question: <Q>\n\n" — strip the surrounding scaffolding.
        marker = "Reviewer question:"
        if marker in content:
            tail = content.split(marker, 1)[1].lstrip()
            # First non-empty line is the question.
            return tail.splitlines()[0].strip() if tail else ""
        return content[:200]
    return ""


def _server_run_for_turn(conn, chat_id: str, turn_id: str) -> str:
    """Diagnostic: which physical server_run_id wrote (the latest rows of)
    this turn. turn_summary is GROUP BY chat_id/case_id/turn_id and does not
    expose server_run_id, so this is a small per-turn lookup (mirrors
    ``_question_for_turn``) rather than a column on that view.
    """
    r = conn.execute(
        "SELECT server_run_id FROM node_trace WHERE chat_id = ? AND turn_id = ? "
        "AND server_run_id IS NOT NULL ORDER BY started_at DESC LIMIT 1",
        (chat_id, turn_id)).fetchone()
    return r["server_run_id"] if r else ""


# ── Templates ───────────────────────────────────────────────────────────────

_REFRESH_META = (
    '{% if refresh_secs %}<meta http-equiv="refresh" content="{{ refresh_secs }}">{% endif %}'
)
_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <!-- Same Blue Box family as the chat app, deeper navy so the two are
       distinguishable in adjacent tabs. Monitoring reads as a live trace: a
       pulse across the box, with a sparkle tying it to AgenticSys. -->
  <rect width="32" height="32" rx="7.5" fill="#00175A"/>
  <path d="M5.5 19.5h4.2l2.6-7.4 3.4 12 2.7-8.3 1.8 3.7h6.3" fill="none"
        stroke="#FFFFFF" stroke-width="2.4" stroke-linecap="round"
        stroke-linejoin="round"/>
  <path d="M24.4 5.2c.35 2.4 1.95 4 4.35 4.35-2.4.35-4 1.95-4.35 4.35-.35-2.4-1.95-4-4.35-4.35 2.4-.35 4-1.95 4.35-4.35z"
        fill="#7FC4FF"/>
</svg>"""
_FAVICON = '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
_REFRESH_BADGE = """
{% if refresh_secs %}
<div style="position:fixed; top:8px; right:8px; background:#fef3c7; color:#92400e;
            padding:4px 10px; border-radius:4px; font-size:11px;
            border:1px solid #fde68a;">
  auto-refresh: {{ refresh_secs }}s
  · <a href="#" onclick="location.reload();return false;" style="color:#92400e;font-weight:600;">↻ refresh now</a>
  · <a href="?norefresh=1" style="color:#92400e;">pause</a>
</div>
{% else %}
<div style="position:fixed; top:8px; right:8px; background:#f3f4f6; color:#6b7280;
            padding:4px 10px; border-radius:4px; font-size:11px;
            border:1px solid #e5e7eb;">
  paused
  · <a href="#" onclick="location.reload();return false;" style="color:#374151;font-weight:600;">↻ refresh now</a>
  · <a href="?" style="color:#6b7280;">resume</a>
</div>
{% endif %}
"""

_STYLE = """
<style>
html, body { margin: 0; padding: 0; }
body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       padding: 24px; color: #1a1a1a; box-sizing: border-box; }
/* Flex layout (not grid) so a draggable splitter can resize the panes
   by adjusting flex-basis directly. Default 1:1; the user can drag the
   splitter to any ratio between 20% / 80%. Choice is persisted to
   localStorage so it survives page refreshes. Window-resize is handled
   automatically because flex percentages adjust to viewport width.
   Stacks below 1200px (see @media). */
.split { display: flex; align-items: stretch; gap: 0; }
.main-col { flex: 1 1 50%; min-width: 0; overflow-x: auto; padding-right: 12px; }
.splitter { flex: 0 0 6px; cursor: col-resize; background: transparent;
            border-left: 1px solid #e5e7eb; border-right: 1px solid #e5e7eb;
            position: relative; transition: background 0.12s; }
.splitter:hover, .splitter.dragging { background: #dbeafe; border-color: #93c5fd; }
.splitter::before { content: ""; position: absolute; left: 50%; top: 50%;
                    transform: translate(-50%, -50%); width: 4px; height: 32px;
                    border-radius: 2px; background: #cbd5e1; }
.splitter:hover::before, .splitter.dragging::before { background: #3b82f6; }
.detail-pane { flex: 1 1 50%; min-width: 0;
               position: sticky; top: 12px; height: calc(100vh - 36px);
               overflow-y: auto; padding: 14px 16px; margin-left: 12px;
               background: #fff;
               border: 1px solid #e5e7eb; border-radius: 6px;
               box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
.detail-pane h2 { margin-top: 0; }
.detail-empty { color: #9ca3af; font-style: italic; padding: 24px; text-align: center; }
.node-detail { display: none; }
.node-detail.active { display: block; }
/* Tabbed Input/Output/Extras within the active detail. Default = Input
   visible. Body class `tab-output` / `tab-extras` flips which section
   is shown across all active details simultaneously. */
.detail-section { display: block; }
.tab-output .input-section,  .tab-extras .input-section  { display: none; }
.tab-input .output-section,  .tab-extras .output-section { display: none; }
.tab-input .extras-section,  .tab-output .extras-section { display: none; }
.tabs { display: flex; gap: 4px; margin: 0 0 12px; border-bottom: 1px solid #e5e7eb; }
.tabs button { background: none; border: none; padding: 8px 16px; cursor: pointer;
               font: 13px/1 -apple-system, sans-serif; color: #6b7280;
               border-bottom: 2px solid transparent; margin-bottom: -1px; }
.tabs button:hover { color: #1f2937; }
.tabs button.active { color: #1f2937; border-bottom-color: #2563eb; font-weight: 600; }
tbody tr.clickable { cursor: pointer; }
tbody tr.selected td, tbody tr.selected th { background: #dbeafe !important; }
tbody tr.clickable:hover td, tbody tr.clickable:hover th { background: #eff6ff; }
@media (max-width: 1199px) {
  .split { flex-direction: column; }
  .main-col { padding-right: 0; }
  .splitter { display: none; }
  .detail-pane { position: static; height: auto; max-height: 70vh; margin-left: 0; }
}
h1 { font-size: 20px; margin: 0 0 16px; }
h2 { font-size: 16px; margin: 24px 0 8px; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
th, td { padding: 6px 10px; border-bottom: 1px solid #e5e7eb;
         font-variant-numeric: tabular-nums; }
td { text-align: left; }
th { background: #f9fafb; font-weight: 600; text-align: center; }
th.right { text-align: center; }  /* override .right on headers — keep header text centered above right-aligned data cells */
tr:hover td { background: #fafbfc; }
.muted { color: #6b7280; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 4px;
       background: #eff6ff; color: #1e40af; font-size: 12px; margin-right: 4px; }
.row-depth-1 td:first-child { padding-left: 28px; }
.row-depth-2 td:first-child { padding-left: 52px; }
.dur-warn { color: #b91c1c; font-weight: 600; }              /* > 10s */
.dur-bad  { color: #7f1d1d; font-weight: 700; background: #fee2e2; }  /* > 50s */
.cache-bad  { color: #b91c1c; font-weight: 600; }                       /* < 30% */
.cache-warn { color: #b45309; }                                         /* < 60% */
.cache-good { color: #15803d; font-weight: 500; }                       /* >= 80% */
.q-cell { max-width: 380px; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; color: #374151; }
details { margin: 8px 0; }
summary { cursor: pointer; padding: 6px 0; font-weight: 500; }
pre { background: #f6f8fa; padding: 12px; border-radius: 4px;
      overflow-x: auto; white-space: pre-wrap; word-break: break-word;
      font: 12px/1.45 SF Mono, Menlo, Consolas, monospace; max-height: 600px; }
.msg { border-left: 3px solid #d1d5db; padding: 8px 12px; margin: 6px 0;
       background: #f9fafb; border-radius: 0 4px 4px 0; }
.msg .role { font-weight: 600; color: #4b5563; font-size: 12px;
             text-transform: uppercase; letter-spacing: 0.5px; }
.msg .content { margin-top: 4px; white-space: pre-wrap; word-break: break-word;
                font: 13px/1.5 SF Mono, Menlo, Consolas, monospace; }
nav { margin-bottom: 16px; font-size: 13px; }
nav a { margin-right: 12px; }
.right { text-align: right; }

/* Turn switcher — horizontal pill bar at the top of each turn page so
   the user can hop between turns in the same chat without round-tripping
   through /chat/<id>. Pills wrap onto multiple lines on narrow screens. */
.turn-switcher { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 18px;
                 padding: 10px 12px; background: #f9fafb;
                 border: 1px solid #e5e7eb; border-radius: 6px; }
.turn-switcher .label { font-size: 12px; color: #6b7280; align-self: center;
                        margin-right: 6px; flex: 0 0 auto; }
.turn-pill { display: inline-flex; align-items: center; gap: 6px;
             padding: 4px 10px; border-radius: 12px; font-size: 12px;
             background: #fff; border: 1px solid #d1d5db; color: #374151;
             text-decoration: none; max-width: 320px;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.turn-pill:hover { background: #eff6ff; border-color: #93c5fd; color: #1e40af;
                   text-decoration: none; }
.turn-pill.current { background: #1d4ed8; color: #fff; border-color: #1d4ed8;
                     font-weight: 600; }
.turn-pill .tid { font-family: SF Mono, Menlo, Consolas, monospace;
                  font-size: 11px; opacity: 0.7; }
.turn-pill.current .tid { opacity: 0.85; }
</style>
"""

_INDEX = """
<!doctype html>
<html><head><meta charset="utf-8">""" + _REFRESH_META + """<title>AgenticSys Monitor</title>""" + _FAVICON + _STYLE + """</head>
<body>""" + _REFRESH_BADGE + """
  <h1>AgenticSys Monitor <span class="muted">· agent run traces · {{ db }}</span></h1>
  <nav><a href="/">conversations</a></nav>
  <h2>Conversations ({{ chats|length }})</h2>
  <table>
    <thead><tr>
      <th>conversation</th><th>case_id</th><th class="right">turns</th>
      <th class="right">total tokens</th><th class="right">cost ($)</th>
      <th>last activity (SGT)</th>
      <th>latest question</th>
    </tr></thead>
    <tbody>
    {% for c in chats %}
      <tr>
        <td>
          {% if c.latest_turn_id %}
            <a href="/turn/{{ c.chat_id }}/{{ c.latest_turn_id }}">{{ c.chat_id }}</a>
            <span class="muted" style="font-size:11px;">
              · <a href="/chat/{{ c.chat_id }}" style="color:#6b7280;">all turns</a>
            </span>
          {% else %}
            <a href="/chat/{{ c.chat_id }}">{{ c.chat_id }}</a>
          {% endif %}
        </td>
        <td>{{ c.case_id }}</td>
        <td class="right">{{ c.n_turns }}</td>
        <td class="right">{{ "{:,}".format(c.total_tokens or 0) }}</td>
        <td class="right">{{ "{:.4f}".format(c.total_cost_usd or 0) }}</td>
        <td class="muted">{{ c.session_ended_at_sg or "" }}</td>
        <td class="q-cell" title="{{ c.latest_question or '' }}">{{ c.latest_question or "—" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</body></html>
"""

_CHAT = """
<!doctype html>
<html><head><meta charset="utf-8">""" + _REFRESH_META + """<title>{{ chat_id }} · AgenticSys Monitor</title>""" + _FAVICON + _STYLE + """</head>
<body>""" + _REFRESH_BADGE + """
  <h1>Conversation: {{ chat_id }}</h1>
  <nav>
    <a href="/">← conversations</a>
    <a href="/state/{{ chat_id }}">cross-turn state →</a>
  </nav>
  <h2>Turns ({{ turns|length }})</h2>
  <table>
    <thead><tr>
      <th>turn_id</th><th>started (SGT)</th>
      <th>question</th>
      <th class="right">duration</th>
      <th class="right">prompt tok</th>
      <th class="right">cached</th>
      <th class="right">completion tok</th>
      <th class="right">cost ($)</th>
      <th class="right">nodes</th>
      <th class="right">failures</th>
      <th>server run</th>
    </tr></thead>
    <tbody>
    {% for t in turns %}
      <tr>
        <td><a href="/turn/{{ chat_id }}/{{ t.turn_id }}">{{ t.turn_id }}</a></td>
        <td class="muted">{{ t.started_at_sg }}</td>
        <td class="q-cell" title="{{ t.question or '' }}">{{ t.question or "—" }}</td>
        <td class="right {{ dur_class(t.duration_s) }}">{{ "%.1fs"|format(t.duration_s or 0) }}</td>
        <td class="right">{{ "{:,}".format(t.total_prompt_tokens or 0) }}</td>
        <td class="right">{{ "{:,}".format(t.total_cached_tokens or 0) }}</td>
        <td class="right">{{ "{:,}".format(t.total_completion_tokens or 0) }}</td>
        <td class="right">{{ "{:.4f}".format(t.total_cost_usd or 0) }}</td>
        <td class="right">{{ t.n_nodes }}</td>
        <td class="right">{{ t.n_failures }}</td>
        <td class="muted" style="font-size:12px;">{{ t.server_run_id or "—" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</body></html>
"""

_NODE_DETAIL_MACRO = """
{%- macro render_node_detail(r) -%}
  <div class="node-detail" id="node-detail-{{ r.id }}">
    <h3 style="margin: 0 0 6px;">{{ r.node }}</h3>
    <p class="muted" style="margin: 0 0 12px;">
      {{ "%.2fs"|format((r.duration_ms or 0)/1000) }}
      {% if r.model %}· {{ r.model }}{% endif %}
      {% if r.prompt_tokens %}· {{ "{:,}".format(r.prompt_tokens) }} in tok{% endif %}
      {% if r.completion_tokens %}· {{ "{:,}".format(r.completion_tokens) }} out tok{% endif %}
      {% if r.cost_usd %}· ${{ "{:.4f}".format(r.cost_usd) }}{% endif %}
      {% if r.children %}· wraps {{ r.children|length }} round{{ "s" if r.children|length != 1 else "" }}{% endif %}
    </p>
    <div class="detail-section input-section">
      {% if r.messages %}
        <h4 style="font-size:13px; margin: 8px 0 4px; color:#4b5563;">Input (messages)</h4>
        {% for m in r.messages %}
          <div class="msg">
            <div class="role">{{ m.role }}</div>
            <div class="content">{{ m.content }}</div>
          </div>
        {% endfor %}
      {% elif r.prompt_excerpt and not r.children %}
        <h4 style="font-size:13px; margin: 8px 0 4px; color:#4b5563;">Prompt (excerpt — row was captured before full-I/O default)</h4>
        <pre>{{ r.prompt_excerpt }}</pre>
      {% elif r.children %}
        <p class="muted" style="font-size:12px;">This is a wrapper node — its actual prompts are on its child round(s). Click a child row to see them.</p>
      {% else %}
        <p class="muted" style="font-size:12px;">No input captured.</p>
      {% endif %}
    </div>
    <div class="detail-section output-section">
      {% if r.output_pretty %}
        <h4 style="font-size:13px; margin: 8px 0 4px; color:#4b5563;">Output (raw)</h4>
        <pre>{{ r.output_pretty }}</pre>
      {% elif r.completion_excerpt and not r.children %}
        <h4 style="font-size:13px; margin: 8px 0 4px; color:#4b5563;">Completion</h4>
        <pre>{{ r.completion_excerpt }}</pre>
      {% else %}
        <p class="muted" style="font-size:12px;">No output captured.</p>
      {% endif %}
    </div>
    {% if r.extra_pretty %}
      <div class="detail-section extras-section">
        <h4 style="font-size:13px; margin: 8px 0 4px; color:#4b5563;">Extra</h4>
        <pre>{{ r.extra_pretty }}</pre>
      </div>
    {% endif %}
  </div>
  {% if r.children %}{% for child in r.children %}{{ render_node_detail(child) }}{% endfor %}{% endif %}
{%- endmacro -%}
"""

_STATE = """
<!doctype html>
<html><head><meta charset="utf-8">""" + _REFRESH_META + """<title>{{ chat_id }} state · AgenticSys Monitor</title>""" + _FAVICON + _STYLE + """</head>
<body>""" + _REFRESH_BADGE + """
  <h1>Cross-turn state: {{ chat_id }}</h1>
  <nav>
    <a href="/">← conversations</a>
    <a href="/chat/{{ chat_id }}">← turns</a>
  </nav>

  <p class="muted">
    Snapshots are taken at end-of-turn after distillers drain, after
    qa_cache + specialist_kb are updated. Rewind drops
    all snapshots for this chat (same lifetime as the trace rows).
  </p>

  <h2>Snapshots ({{ snapshots|length }})</h2>
  <table>
    <thead><tr>
      <th>turn_id</th><th>taken (SGT)</th>
      <th class="right">qa_cache</th>
      <th class="right">KB specialists</th>
      <th class="right">KB KPs</th>
    </tr></thead>
    <tbody>
    {% for s in snapshots %}
      <tr>
        <td><a href="/turn/{{ chat_id }}/{{ s.turn_id }}">{{ s.turn_id }}</a></td>
        <td class="muted">{{ s.taken_at_sg }}</td>
        <td class="right">{{ s.qa_cache_n }}</td>
        <td class="right">{{ s.kb_specialists_n }}</td>
        <td class="right">{{ s.kb_kps_n }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <h2>Latest snapshot detail</h2>
  {% if latest %}
    <details open>
      <summary><strong>qa_cache</strong> · {{ latest.qa_cache_n }} entries</summary>
      <pre>{{ latest.qa_cache_pretty }}</pre>
    </details>
    <details>
      <summary><strong>specialist_kb</strong> · {{ latest.kb_specialists_n }} specialists, {{ latest.kb_kps_n }} knowledge points</summary>
      <pre>{{ latest.kb_pretty }}</pre>
    </details>
  {% else %}
    <p class="muted">No snapshots yet. Restart the server and run a turn to populate.</p>
  {% endif %}
</body></html>
"""

_TURN = _NODE_DETAIL_MACRO + """
<!doctype html>
<html><head><meta charset="utf-8">""" + _REFRESH_META + """<title>{% if question %}{{ question[:60] }} · {% endif %}{{ turn_id }} · AgenticSys Monitor</title>""" + _FAVICON + _STYLE + """</head>
<body>""" + _REFRESH_BADGE + """
  <h1>
    {% if question %}
      {{ question }}
      <div class="muted" style="font-size:13px;font-weight:normal;margin-top:4px;">
        turn <code>{{ turn_id }}</code> · {{ chat_id }}
        {% if flat_rows and flat_rows[0].server_run_id %}· <span class="muted">server run {{ flat_rows[0].server_run_id }}</span>{% endif %}
      </div>
    {% else %}
      Turn: {{ turn_id }}
      <span class="muted" style="font-size:14px;font-weight:normal;">— {{ chat_id }}</span>
      {% if flat_rows and flat_rows[0].server_run_id %}<span class="muted" style="font-size:13px;font-weight:normal;">· server run {{ flat_rows[0].server_run_id }}</span>{% endif %}
    {% endif %}
  </h1>
  <nav>
    <a href="/">← conversations</a>
    <a href="/chat/{{ chat_id }}">turns list</a>
    <a href="/state/{{ chat_id }}">cross-turn state</a>
  </nav>

  {% if sibling_turns and sibling_turns|length > 1 %}
  <div class="turn-switcher">
    <span class="label">Turns in this chat ({{ sibling_turns|length }}):</span>
    {% for t in sibling_turns %}
      <a class="turn-pill {{ 'current' if t.turn_id == turn_id else '' }}"
         href="/turn/{{ chat_id }}/{{ t.turn_id }}"
         title="{{ t.question or t.turn_id }} · {{ t.started_at_sg }} · {{ '%.1fs'|format(t.duration_s or 0) }}">
        <span class="tid">{{ t.turn_id[:8] }}</span>
        <span>{{ (t.question or '(no question captured)')[:60] }}{% if (t.question or '')|length > 60 %}…{% endif %}</span>
      </a>
    {% endfor %}
  </div>
  {% endif %}

  <h2>Summary</h2>
  <table style="width: auto;">
    <thead>
      <tr>
        <th>wall-clock</th>
        <th>prompt tokens</th>
        <th>cached tokens</th>
        <th>completion tokens</th>
        <th>cost</th>
        <th>nodes</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td class="right">{{ "%.1fs"|format(total_s) }}</td>
        <td class="right">{{ "{:,}".format(total_in) }}</td>
        <td class="right">{{ "{:,}".format(total_cached) }} <span class="muted">({{ "{:.1%}".format(cache_ratio) }})</span></td>
        <td class="right">{{ "{:,}".format(total_out) }}</td>
        <td class="right">${{ "{:.4f}".format(total_cost) }}</td>
        <td class="right">{{ flat_rows|length }}</td>
      </tr>
    </tbody>
  </table>

  <div class="split">
  <div class="main-col">
  <h2>Nodes (tree order) <span class="muted" style="font-weight:normal;font-size:13px;">click a row → prompt/completion shows on the right</span></h2>
  <table>
    <thead><tr>
      <th>node</th>
      <th class="right">duration</th>
      <th class="right">queue</th>
      <th class="right">llm</th>
      <th class="right">ttft</th>
      <th class="right">in tok</th>
      <th class="right">cached</th>
      <th class="right">cache %</th>
      <th class="right">out tok</th>
      <th class="right" title="Tool calls emitted in this round. Multiple rounds each with only 1 tool call means batching was missed.">tools</th>
      <th class="right">$</th>
      <th>tags</th>
      <th>outcome</th>
    </tr></thead>
    <tbody>
    {% for r in flat_rows %}
      <tr class="row-depth-{{ r.depth }} clickable" data-node-id="{{ r.id }}">
        <td>{{ "↳ " if r.depth else "" }}{{ r.node }}</td>
        <td class="right {{ dur_class((r.duration_ms or 0)/1000) }}">{{ "%.2fs"|format((r.duration_ms or 0)/1000) }}</td>
        <td class="right muted">{{ ms(r.queue_wait_ms) }}</td>
        <td class="right muted">{{ ms(r.llm_call_ms) }}</td>
        <td class="right muted">{{ ms(r.ttft_ms) }}</td>
        <td class="right">{{ "{:,}".format(r.prompt_tokens) if r.prompt_tokens else "" }}</td>
        <td class="right muted">{{ "{:,}".format(r.cached_input_tokens) if r.cached_input_tokens else "" }}</td>
        <td class="right {{ cache_class(r.cache_pct) }}">{{ "{:.0%}".format(r.cache_pct) if r.cache_pct is not none else "" }}</td>
        <td class="right">{{ "{:,}".format(r.completion_tokens) if r.completion_tokens else "" }}</td>
        <td class="{{ 'dur-warn' if r.batching_missed else '' }}"
            title="{{ 'BATCHING MISSED: parent has multiple rounds and this round emitted only 1 tool call.' if r.batching_missed else '' }}">
          {% if r.n_tool_calls is not none %}
            <span class="muted">{{ r.n_tool_calls }}</span>
            {% if r.tool_call_names %}
              <span style="font-family: SF Mono, Menlo, Consolas, monospace; font-size: 11px;">·
                {{ r.tool_call_names|join(", ") }}</span>
            {% endif %}
          {% endif %}
        </td>
        <td class="right">{{ "{:.4f}".format(r.cost_usd) if r.cost_usd else "" }}</td>
        <td>
          {% for t in r.tag_list %}<span class="tag">{{ t }}</span>{% endfor %}
        </td>
        <td>{{ r.outcome }}{% if r.error_type %} ({{ r.error_type }}){% endif %}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <h2>Cross-turn memory <span class="muted" style="font-weight:normal;font-size:13px;">click a row → full content shows on the right</span></h2>
  {% if snapshot %}
    <p class="muted" style="font-size:13px;margin:0 0 8px;">Snapshot taken at end of this turn — what the next turn will see. Taken {{ snapshot.taken_at_sg }}.</p>
    <table>
      <thead><tr>
        <th>bucket</th><th class="right">size</th><th>what it holds</th>
      </tr></thead>
      <tbody>
        <tr class="clickable" data-node-id="xt-qa">
          <td><strong>qa_cache</strong></td>
          <td class="right">{{ snapshot.qa_cache_n }} entries</td>
          <td class="muted">conversation cache of prior Q→A; serves replays on near-duplicate questions</td>
        </tr>
        <tr class="clickable" data-node-id="xt-kb">
          <td><strong>specialist_kb</strong></td>
          <td class="right">{{ snapshot.kb_kps_n }} KPs / {{ snapshot.kb_specialists_n }} specialists</td>
          <td class="muted">distilled knowledge points; preface each specialist's next prompt</td>
        </tr>
      </tbody>
    </table>
  {% else %}
    <p class="muted">No snapshot for this turn yet (snapshot is written at end-of-turn after distillers drain). If this turn is still running, refresh in a moment. If the turn completed but no snapshot appeared, the server may have run an older code revision — restart it.</p>
  {% endif %}
  </div>

  <div class="splitter" id="splitter" title="Drag to resize · double-click to reset to 50 / 50"></div>

  <div class="detail-pane">
    <h2>Prompt &amp; Completions</h2>
    <div class="tabs" id="detail-tabs">
      <button data-tab="input" class="active">Input</button>
      <button data-tab="output">Output</button>
      <button data-tab="extras">Extras</button>
    </div>
    <div id="detail-empty" class="detail-empty">Click a row on the left.</div>
    {% for root in tree %}{{ render_node_detail(root) }}{% endfor %}
    {% if snapshot %}
      <div class="node-detail" id="node-detail-xt-qa">
        <h3 style="margin:0 0 6px;">qa_cache</h3>
        <p class="muted" style="margin:0 0 12px;">{{ snapshot.qa_cache_n }} entries · cached at {{ snapshot.taken_at_sg }}</p>
        <pre>{{ snapshot.qa_cache_pretty }}</pre>
      </div>
      <div class="node-detail" id="node-detail-xt-kb">
        <h3 style="margin:0 0 6px;">specialist_kb</h3>
        <p class="muted" style="margin:0 0 12px;">{{ snapshot.kb_specialists_n }} specialists · {{ snapshot.kb_kps_n }} knowledge points</p>
        <pre>{{ snapshot.kb_pretty }}</pre>
      </div>
    {% endif %}
  </div>
  </div>

  <script>
    (function(){
      function selectNode(id){
        document.querySelectorAll('tbody tr.selected').forEach(function(el){
          el.classList.remove('selected');
        });
        document.querySelectorAll('.node-detail.active').forEach(function(el){
          el.classList.remove('active');
        });
        var empty = document.getElementById('detail-empty');
        if (empty) empty.style.display = 'none';
        var row = document.querySelector('tbody tr[data-node-id="' + id + '"]');
        if (row) row.classList.add('selected');
        var detail = document.getElementById('node-detail-' + id);
        if (detail) {
          detail.classList.add('active');
          var pane = document.querySelector('.detail-pane');
          if (pane) pane.scrollTop = 0;
        }
      }
      function selectTab(tab){
        document.body.classList.remove('tab-input', 'tab-output', 'tab-extras');
        document.body.classList.add('tab-' + tab);
        document.querySelectorAll('#detail-tabs button').forEach(function(b){
          b.classList.toggle('active', b.dataset.tab === tab);
        });
        // Also scroll the pane to top so the user sees the section start.
        var pane = document.querySelector('.detail-pane');
        if (pane) pane.scrollTop = 0;
      }
      document.querySelectorAll('tbody tr.clickable').forEach(function(row){
        row.addEventListener('click', function(){
          selectNode(row.dataset.nodeId);
        });
      });
      document.querySelectorAll('#detail-tabs button').forEach(function(b){
        b.addEventListener('click', function(){ selectTab(b.dataset.tab); });
      });
      // Defaults: tab=input, first row selected.
      selectTab('input');
      var first = document.querySelector('tbody tr.clickable');
      if (first) selectNode(first.dataset.nodeId);

      // ── Draggable splitter ─────────────────────────────────────────
      // Adjusts the flex-basis of the left and right panes as the user
      // drags. Ratio persists to localStorage. Double-click resets to 50/50.
      var splitter = document.getElementById('splitter');
      var split    = document.querySelector('.split');
      var leftPane = document.querySelector('.split > .main-col');
      var rightPane = document.querySelector('.split > .detail-pane');
      var STORAGE_KEY = 'node_trace_left_pct';

      function applyRatio(pct){
        // Clamp to a sensible range so neither pane disappears.
        pct = Math.max(20, Math.min(80, pct));
        if (leftPane)  leftPane.style.flex  = '0 0 ' + pct + '%';
        if (rightPane) rightPane.style.flex = '0 0 ' + (100 - pct) + '%';
      }

      // Restore previous ratio (if any) on load.
      try {
        var saved = parseFloat(localStorage.getItem(STORAGE_KEY));
        if (!isNaN(saved)) applyRatio(saved);
      } catch (e) {}

      var dragging = false;
      function onMove(ev){
        if (!dragging || !split) return;
        var rect = split.getBoundingClientRect();
        var x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
        var pct = (x / rect.width) * 100;
        applyRatio(pct);
      }
      function onUp(){
        if (!dragging) return;
        dragging = false;
        document.body.style.userSelect = '';
        if (splitter) splitter.classList.remove('dragging');
        // Persist final ratio.
        try {
          if (leftPane) {
            var pct = parseFloat(leftPane.style.flex.replace(/.*?(\d+(?:\.\d+)?)%.*/, '$1'));
            if (!isNaN(pct)) localStorage.setItem(STORAGE_KEY, pct.toFixed(1));
          }
        } catch (e) {}
      }
      if (splitter) {
        splitter.addEventListener('mousedown', function(ev){
          dragging = true;
          document.body.style.userSelect = 'none';
          splitter.classList.add('dragging');
          ev.preventDefault();
        });
        splitter.addEventListener('dblclick', function(){
          applyRatio(50);
          try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
        });
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
        // Touch support
        splitter.addEventListener('touchstart', function(ev){
          dragging = true; splitter.classList.add('dragging');
        }, {passive: true});
        document.addEventListener('touchmove', onMove, {passive: true});
        document.addEventListener('touchend', onUp);
      }
    })();
  </script>
</body></html>
"""


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/favicon.svg")
def favicon():
    return Response(_FAVICON_SVG, mimetype="image/svg+xml",
                    headers={"Cache-Control": "max-age=86400"})


@app.get("/")
def index():
    conn = open_db(_db())
    try:
        chats = [dict(r) for r in conn.execute(
            "SELECT * FROM session_summary ORDER BY session_ended_at DESC"
        ).fetchall()]
    except Exception:
        chats = []
    # Decorate: SGT timestamp + the latest turn's id/question per chat.
    # Surfacing latest_turn_id lets the chat-row link jump straight to
    # that turn (where the turn-switcher takes over for navigation), so
    # most audits never hit /chat/<id> at all.
    for c in chats:
        c["session_ended_at_sg"] = _iso_to_sgt(c.get("session_ended_at"))
        latest = conn.execute(
            "SELECT turn_id FROM node_trace WHERE chat_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (c["chat_id"],),
        ).fetchone()
        c["latest_turn_id"] = latest["turn_id"] if latest else None
        c["latest_question"] = (
            _question_for_turn(conn, c["chat_id"], latest["turn_id"])
            if latest else ""
        )
    return render_template_string(
        _INDEX, chats=chats, db=str(_db()), refresh_secs=_refresh_secs(),
    )


@app.get("/chat/<chat_id>")
def chat(chat_id: str):
    conn = open_db(_db())
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM turn_summary WHERE chat_id = ? "
            "ORDER BY turn_started_at DESC",
            (chat_id,),
        ).fetchall()]
    except sqlite3.OperationalError:
        rows = []
    # turn_summary doesn't carry duration_s, compute from start/end.
    for t in rows:
        s, e = t.get("turn_started_at"), t.get("turn_ended_at")
        try:
            t["duration_s"] = (
                datetime.fromisoformat(e) - datetime.fromisoformat(s)
            ).total_seconds() if s and e else 0
        except Exception:
            t["duration_s"] = 0
        t["started_at_sg"] = _iso_to_sgt(s)
        t["question"] = _question_for_turn(conn, chat_id, t["turn_id"])
        t["server_run_id"] = _server_run_for_turn(conn, chat_id, t["turn_id"])
    if not rows:
        return redirect("/")
    return render_template_string(
        _CHAT, chat_id=chat_id, turns=rows, dur_class=_dur_class,
        refresh_secs=_refresh_secs(),
    )


def _extract_tool_calls(output_json: str | None) -> tuple[int | None, list[str]]:
    """Pull the tool_calls count + per-call function names out of a
    ChatCompletion JSON payload.

    Returns ``(count, names)`` where ``names`` is in call order. Returns
    ``(None, [])`` when output isn't stored / can't be parsed / isn't a
    chat-completion payload.
    """
    if not output_json:
        return None, []
    try:
        obj = json.loads(output_json)
    except Exception:
        return None, []
    if not isinstance(obj, dict):
        return None, []
    choices = obj.get("choices") or []
    if not choices:
        return None, []
    msg = (choices[0] or {}).get("message") or {}
    tcs = msg.get("tool_calls") or []
    if not isinstance(tcs, list):
        return None, []
    names: list[str] = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        n = fn.get("name") if isinstance(fn, dict) else None
        if n:
            names.append(str(n))
    return len(tcs), names


def _decorate(r: dict) -> None:
    """Mutate row in place: parse tags/messages_json, pretty-print extras."""
    try:
        r["tag_list"] = json.loads(r.get("tags") or "[]")
    except Exception:
        r["tag_list"] = []
    r["messages"] = []
    if r.get("messages_json"):
        try:
            parsed = json.loads(r["messages_json"])
            if isinstance(parsed, list):
                def _msg_role(m: dict) -> str:
                    role = m.get("role")
                    if role:
                        return role
                    t = m.get("type", "")
                    if t == "function_call":
                        return f"tool_call → {m.get('name', '?')}"
                    if t == "function_call_output":
                        return "tool_result"
                    return t or "?"

                def _msg_content(m: dict) -> str:
                    c = m.get("content")
                    if c:
                        return str(c)
                    if m.get("type") == "function_call":
                        args = m.get("arguments", "")
                        return str(args)[:500] if args else "(no args)"
                    if m.get("type") == "function_call_output":
                        out = m.get("output", "")
                        return str(out)[:500] if out else "(empty)"
                    return ""

                r["messages"] = [
                    {"role": _msg_role(m), "content": _msg_content(m)}
                    for m in parsed if isinstance(m, dict)
                ]
        except Exception:
            pass
    r["output_pretty"] = None
    if r.get("output_json"):
        try:
            r["output_pretty"] = json.dumps(
                json.loads(r["output_json"]), indent=2, ensure_ascii=False,
            )
        except Exception:
            r["output_pretty"] = r["output_json"]
    r["extra_pretty"] = None
    if r.get("extra_json"):
        try:
            r["extra_pretty"] = json.dumps(
                json.loads(r["extra_json"]), indent=2, ensure_ascii=False,
            )
        except Exception:
            r["extra_pretty"] = r["extra_json"]
    n_tc, names = _extract_tool_calls(r.get("output_json"))
    r["n_tool_calls"] = n_tc
    r["tool_call_names"] = names


def _build_tree(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (tree_roots, flat_rows_in_tree_order). Each row gains a
    ``children`` list.

    Tree shape: every depth-0 row (wrapper — chat.*, orchestrator,
    specialist.*, distiller.*) is a ROOT. Depth-1+ rows (rounds) attach
    to their NEAREST depth-0 ancestor via the parent_id chain.

    Rationale: the DB stores parent_id based on the contextvar at the
    moment of insertion, which can produce spurious links between
    wrappers (e.g. ``distiller.spend_payments`` ends up with
    ``parent_id = orchestrator`` because asyncio.Task copied that
    context). For the viewer, those wrapper-to-wrapper links would
    nest a distiller's children visually under the orchestrator, making
    later orchestrator rounds appear under the distiller. Resolving
    depth-1 nodes to their nearest depth-0 ancestor sidesteps the bug
    and matches the user's mental model: each wrapper is a top-level
    concept; its rounds nest below.
    """
    by_id = {r["id"]: {**r, "children": []} for r in rows}

    # Depth-0 rows are tree roots; depth-1+ rows attach to their nearest
    # depth-0 ancestor.
    roots: list[dict] = [
        r for r in by_id.values() if (r.get("depth") or 0) == 0
    ]
    for r in by_id.values():
        if (r.get("depth") or 0) == 0:
            continue
        cursor = r
        while True:
            pid = cursor.get("parent_id")
            if not pid or pid not in by_id:
                # Orphaned round (parent wasn't in the row set) — make it
                # a root in its own right so it still renders.
                roots.append(r)
                break
            parent = by_id[pid]
            if (parent.get("depth") or 0) == 0:
                parent["children"].append(r)
                break
            cursor = parent

    # Stable sort children by started_at so round_1 / round_2 stay ordered.
    def _sort_kids(node: dict) -> None:
        node["children"].sort(key=lambda c: c.get("started_at") or "")
        for c in node["children"]:
            _sort_kids(c)
    for root in roots:
        _sort_kids(root)
    roots.sort(key=lambda r: r.get("started_at") or "")

    flat: list[dict] = []
    def _walk(n: dict) -> None:
        flat.append(n)
        for c in n["children"]:
            _walk(c)
    for root in roots:
        _walk(root)
    return roots, flat


@app.get("/turn/<chat_id>/<turn_id>")
def turn(chat_id: str, turn_id: str):
    conn = open_db(_db())
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM node_trace WHERE chat_id = ? AND turn_id = ? "
            "ORDER BY started_at",
            (chat_id, turn_id),
        ).fetchall()]
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        return redirect("/")

    for r in rows:
        _decorate(r)
        p_tok = r.get("prompt_tokens") or 0
        c_tok = r.get("cached_input_tokens") or 0
        r["cache_pct"] = (c_tok / p_tok) if p_tok else None

    tree, flat_rows = _build_tree(rows)

    # Flag batching opportunities: when a parent has 2+ child rounds that
    # each emit exactly 1 tool call AND use the same tool (likely
    # independent calls that could have been batched). Sequential calls
    # to DIFFERENT tools (e.g., summarize_trend → query_table) are often
    # dependencies, not missed batching.
    for root in tree:
        children = root.get("children") or []
        single_tool_rounds = [c for c in children if c.get("n_tool_calls") == 1]
        if len(single_tool_rounds) >= 2:
            tool_names = [
                (c.get("tool_call_names") or ["?"])[0] for c in single_tool_rounds
            ]
            if len(set(tool_names)) == 1:
                for child in single_tool_rounds:
                    child["batching_missed"] = True

    total_in = sum(r.get("prompt_tokens") or 0 for r in flat_rows)
    total_out = sum(r.get("completion_tokens") or 0 for r in flat_rows)
    total_cached = sum(r.get("cached_input_tokens") or 0 for r in flat_rows)
    total_cost = sum(r.get("cost_usd") or 0.0 for r in flat_rows)
    total_ms = sum(r.get("duration_ms") or 0 for r in flat_rows if r.get("depth") == 0)

    # End-of-turn snapshot of CaseSession cross-turn memory for THIS turn.
    # The viewer renders it inline at the bottom of the page so the
    # reviewer can see what the next turn will inherit without leaving
    # the page. A separate /state/<chat_id> view still exists for the
    # history-of-snapshots overview.
    snapshot = None
    try:
        snap_row = conn.execute(
            "SELECT * FROM session_snapshot WHERE chat_id = ? AND turn_id = ? "
            "ORDER BY taken_at DESC LIMIT 1",
            (chat_id, turn_id),
        ).fetchone()
    except sqlite3.OperationalError:
        snap_row = None
    if snap_row:
        snapshot = dict(snap_row)
        snapshot["taken_at_sg"] = _iso_to_sgt(snapshot.get("taken_at"))
        for src, dst in (("qa_cache_json", "qa_cache_pretty"),
                         ("specialist_kb_json", "kb_pretty")):
            raw = snapshot.get(src)
            if raw:
                try:
                    snapshot[dst] = json.dumps(
                        json.loads(raw), indent=2, ensure_ascii=False,
                    )
                except Exception:
                    snapshot[dst] = raw
            else:
                snapshot[dst] = "(empty)"

    # Sibling turns in this chat — power the turn-switcher pill bar at
    # the top so you can hop directly between turns without going back
    # to /chat/<id>. Ordered by start time (oldest → newest, left → right).
    sibling_turns: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT * FROM turn_summary WHERE chat_id = ? "
            "ORDER BY turn_started_at ASC",
            (chat_id,),
        ).fetchall()
        for tr in rows:
            t = dict(tr)
            s, e = t.get("turn_started_at"), t.get("turn_ended_at")
            try:
                t["duration_s"] = (
                    datetime.fromisoformat(e) - datetime.fromisoformat(s)
                ).total_seconds() if s and e else 0
            except Exception:
                t["duration_s"] = 0
            t["started_at_sg"] = _iso_to_sgt(s)
            t["question"] = _question_for_turn(conn, chat_id, t["turn_id"])
            sibling_turns.append(t)
    except sqlite3.OperationalError:
        pass

    # Reuse the sibling-turns lookup to find THIS turn's question (the
    # one we'll use as the page title) without a second SQL probe.
    this_question = next(
        (t.get("question") for t in sibling_turns if t.get("turn_id") == turn_id),
        "",
    )

    return render_template_string(
        _TURN,
        chat_id=chat_id, turn_id=turn_id,
        question=this_question,
        tree=tree, flat_rows=flat_rows,
        total_in=total_in, total_out=total_out, total_cached=total_cached,
        cache_ratio=(total_cached / total_in) if total_in else 0,
        total_cost=total_cost, total_s=total_ms / 1000,
        ms=lambda v: f"{int(v)}ms" if v else "",
        dur_class=_dur_class,
        cache_class=_cache_class,
        snapshot=snapshot,
        sibling_turns=sibling_turns,
        refresh_secs=_refresh_secs(),
    )


@app.get("/state/<chat_id>")
def state(chat_id: str):
    conn = open_db(_db())
    try:
        snaps = [dict(r) for r in conn.execute(
            "SELECT * FROM session_snapshot WHERE chat_id = ? "
            "ORDER BY taken_at DESC",
            (chat_id,),
        ).fetchall()]
    except Exception:
        snaps = []
    for s in snaps:
        s["taken_at_sg"] = _iso_to_sgt(s.get("taken_at"))
    latest = None
    if snaps:
        latest = dict(snaps[0])
        for src_key, dst_key in (("qa_cache_json", "qa_cache_pretty"),
                                 ("specialist_kb_json", "kb_pretty")):
            raw = latest.get(src_key)
            if raw:
                try:
                    latest[dst_key] = json.dumps(
                        json.loads(raw), indent=2, ensure_ascii=False,
                    )
                except Exception:
                    latest[dst_key] = raw
            else:
                latest[dst_key] = "(empty)"
    return render_template_string(
        _STATE, chat_id=chat_id, snapshots=snaps, latest=latest,
        refresh_secs=_refresh_secs(),
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=3002)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--db", default=str(_DEFAULT_DB))
    args = p.parse_args()
    app.config["NODE_TRACE_DB"] = args.db
    resolved = Path(args.db).resolve()
    print(f"[node_trace viewer] http://{args.host}:{args.port}  db={resolved}")
    if not resolved.exists():
        print(f"[node_trace viewer] note: {resolved} does not exist yet — "
              f"will be created on first read. If the server is writing to a "
              f"different path, set NODE_TRACE_DB or pass --db to match.")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
