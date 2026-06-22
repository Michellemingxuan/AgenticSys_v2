"""Per-field provenance baselines so reconciliation never clobbers human edits.

Sidecar JSON keeps the prompt-facing profile YAML clean. A field is
"agent-owned" if its current profile value still equals what the agent last
wrote; if it differs, a human edited it and the agent must not overwrite it.
"""
from __future__ import annotations

import json
import os


class Provenance:
    def __init__(self, path: str):
        self._path = path
        self._data: dict = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._data = json.load(f)

    def is_agent_owned(self, table: str, col: str, field: str, current_value) -> bool:
        baseline = self._data.get(table, {}).get(col, {})
        if field not in baseline:
            return True  # never written by the agent → safe to write
        return baseline[field] == current_value

    def record(self, table: str, col: str, field: str, value) -> None:
        self._data.setdefault(table, {}).setdefault(col, {})[field] = value

    def save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True, default=str)
