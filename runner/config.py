"""Shared runtime config for the server + turn runner — env-derived constants
and the node-trace singleton. A neutral module both `server.py` and
`runner/turn/*` import, so `runner/` never imports `server`."""
from __future__ import annotations

import os
from pathlib import Path

from tools.node_trace import NodeTraceStore

PILLAR = os.environ.get("PILLAR", "credit_risk")

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"

# Round-1 (team-planning) watchdog. The orchestrator's first LLM call decides
# which specialists to dispatch; it normally returns in ~3s, but under heavy
# private-env (safechain) traffic it can stall up to ~100s — well under the
# 180s per-call cap, so it isn't aborted and the user just waits. A fresh call
# (manual rewind/retry) almost always returns fast, so we bound the planning
# phase and auto-retry on stall. Generous margin over the ~3s norm; env-tunable.
_ORCH_PLAN_TIMEOUT_S = float(os.environ.get("ORCH_PLAN_TIMEOUT_S", "25"))

# Tighter fence around the screen phase specifically. Screen is one
# `redact` LLM call (often skipped) + one `relevance_check` LLM call —
# normally <5s end-to-end per the project's performance-targets memory.
# A 30s ceiling catches a hung LLM (most often on the safechain backend
# in private env, where transient HTTP-pool exhaustion strands a call
# that the 360s turn fence eventually catches but feels like forever
# from the user's POV). On screen timeout we surface a clear "screen
# took too long" error to the user instead of leaving them watching a
# silent "question check" spinner for 6 minutes. Real failure mode the
# user reported: "stuck in question check, refresh doesn't help, need
# to restart the server or clear history multiple times."
_SCREEN_TIMEOUT_S = float(os.environ.get("SCREEN_TIMEOUT_S", "30"))

# Cap the prior-questions list sent into the relevance_check skill.
# Without this, the prompt for the screen LLM grows as the qa_cache
# fills (up to 64 entries), and on the safechain backend a larger
# prompt is more likely to hit transient HTTP timeouts mid-call. 12
# most recent is plenty for near-duplicate detection on a single
# review session — older questions are unlikely to be re-asked
# verbatim and the cost-of-missing-one is small (the orchestrator
# still re-answers correctly, just without the cache shortcut).
_PRIOR_QUESTIONS_FOR_SCREEN = int(
    os.environ.get("PRIOR_QUESTIONS_FOR_SCREEN", "12")
)

# NodeTrace SQLite store — one per process, shared across sessions.
# Set NODE_TRACE_DISABLE=1 to turn the layer off entirely (escape hatch).
# Expand both ``~`` and ``$VAR`` references so .env values like
# ``NODE_TRACE_DB=$HOME/agenticsys-traces/node_traces.db`` or
# ``NODE_TRACE_DB=~/agenticsys-traces/node_traces.db`` resolve to a real
# absolute path. Without this, python-dotenv hands us the literal string
# and SQLite cheerfully creates a ``$HOME`` directory next to CWD.
_NODE_TRACE_DB_PATH = os.path.expanduser(os.path.expandvars(
    os.environ.get("NODE_TRACE_DB", "logs/node_traces.db")
))
_NODE_TRACE_STORE: NodeTraceStore | None = (
    NodeTraceStore(db_path=_NODE_TRACE_DB_PATH)
    if os.environ.get("NODE_TRACE_DISABLE") != "1"
    else None
)
