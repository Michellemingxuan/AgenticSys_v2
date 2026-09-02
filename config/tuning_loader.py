"""Apply config/tuning.yaml into os.environ at process start.

The memory/context knobs (EPISODIC_TURNS, AMEM_CONSOLIDATE_EVERY_N,
AMEM_ACTIVE_KP_THRESHOLD, …) are read via os.environ.get(...) at IMPORT time in
their owning modules. This loader turns the friendly YAML into those env vars,
using setdefault semantics — an inline env var or a .env entry already present
is NOT overridden, so one-off `EPISODIC_TURNS=2 python server.py` still wins.

server.py calls apply_tuning() once, right after loading .env and BEFORE any
module that reads these values is imported.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - yaml ships with the project (PyYAML)
    yaml = None

# Dot-path in the YAML  ->  environment variable name.
_MAP = {
    "memory.episodic_turns": "EPISODIC_TURNS",
    "memory.consolidate_every_n_turns": "AMEM_CONSOLIDATE_EVERY_N",
    "memory.active_kp_threshold": "AMEM_ACTIVE_KP_THRESHOLD",
    "memory.active_kp_keep": "AMEM_ACTIVE_KP_KEEP",
    # Timeouts (seconds). Same setdefault semantics — an inline env var wins,
    # so `SPECIALIST_TIMEOUT_S=60 python server.py` still overrides the YAML.
    "timeouts.turn_wall_clock_s": "TURN_WALL_CLOCK_S",
    "timeouts.queued_turn_max_wait_s": "QUEUED_TURN_MAX_WAIT_S",
    "timeouts.screen_s": "SCREEN_TIMEOUT_S",
    "timeouts.screen_stall_retry_s": "SCREEN_STALL_RETRY_S",
    "timeouts.orch_plan_s": "ORCH_PLAN_TIMEOUT_S",
    "timeouts.reviewer_s": "REVIEWER_TIMEOUT_S",
    "timeouts.specialist_s": "SPECIALIST_TIMEOUT_S",
    "timeouts.report_agent_s": "REPORT_AGENT_TIMEOUT_S",
    "timeouts.distiller_s": "DISTILLER_TIMEOUT_S",
    "timeouts.distiller_drain_s": "DISTILLER_DRAIN_TIMEOUT_S",
    "timeouts.safechain_call_s": "SAFECHAIN_CALL_TIMEOUT_S",
    "timeouts.safechain_stall_retry_s": "SAFECHAIN_STALL_RETRY_S",
    "timeouts.amem_read_s": "AMEM_READ_TIMEOUT_S",
    "timeouts.amem_write_s": "AMEM_WRITE_TIMEOUT_S",
    "timeouts.amem_active_load_s": "AMEM_ACTIVE_LOAD_TIMEOUT_S",
    # Knowledge base (prior-case retrieval). `enabled` is the master switch;
    # an empty `client` / `json_path` is left as the empty string on purpose,
    # so "configured but blank" and "absent" behave identically.
    "knowledge_base.enabled": "KNOWLEDGE_BASE_ENABLED",
    "knowledge_base.client": "KNOWLEDGE_BASE_CLIENT",
    "knowledge_base.json_path": "KNOWLEDGE_BASE_JSON",
    "knowledge_base.timeout_s": "KNOWLEDGE_BASE_TIMEOUT_S",
    "knowledge_base.max_clusters": "KNOWLEDGE_BASE_MAX_CLUSTERS",
    "knowledge_base.max_bullets": "KNOWLEDGE_BASE_MAX_BULLETS",
    "knowledge_base.text_chars": "KNOWLEDGE_BASE_TEXT_CHARS",
    "knowledge_base.answer_chars": "KNOWLEDGE_BASE_ANSWER_CHARS",
    "knowledge_base.history_turns": "KNOWLEDGE_BASE_HISTORY_TURNS",
}

_DEFAULT_PATH = Path(__file__).resolve().parent / "tuning.yaml"


def _dig(data, dotted: str):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def apply_tuning(path: str | os.PathLike | None = None) -> dict[str, str]:
    """Set os.environ defaults from the tuning YAML (setdefault: an existing env
    var wins). Never raises. Returns the {ENV_NAME: value} actually applied."""
    applied: dict[str, str] = {}
    if yaml is None:
        return applied
    p = Path(path) if path else _DEFAULT_PATH
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return applied
    for dotted, env_name in _MAP.items():
        val = _dig(data, dotted)
        if val is None:
            continue
        if env_name in os.environ:
            continue                       # inline / .env already set it → wins
        os.environ[env_name] = str(val)
        applied[env_name] = str(val)
    return applied
