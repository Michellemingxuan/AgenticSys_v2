#!/usr/bin/env bash
# Start the AgenticSys_v2 backend (Flask API + SSE) and the trace viewer.
# Option A deploy on an air-gapped server using the safechain LLM backend.
#
# Usage:
#   ./run.sh                 # run in foreground
#   nohup ./run.sh > server.log 2>&1 &   # or run under tmux/systemd to survive logout
set -euo pipefail
cd "$(dirname "$0")"

# --- Network: 0.0.0.0 so other machines on the internal network can reach us.
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-3001}"               # backend API
export TRACE_VIEWER_PORT="${TRACE_VIEWER_PORT:-3002}"   # debug viewer (same HOST)

# --- LLM: safechain (no public internet; talks to the internal model endpoint).
export LLM_BACKEND="${LLM_BACKEND:-safechain}"
export MODEL="${MODEL:-gpt-4.1}"
export SAFECHAIN_MODEL="${SAFECHAIN_MODEL:-gpt-4.1}"

# Activate the venv if present (created during setup: python -m venv .venv).
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "[run.sh] backend  -> http://${HOST}:${PORT}   (LLM_BACKEND=${LLM_BACKEND}, MODEL=${MODEL})"
echo "[run.sh] trace UI -> http://${HOST}:${TRACE_VIEWER_PORT}   (set TRACE_VIEWER_DISABLE=1 to skip)"
exec python server.py
