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
