from __future__ import annotations

import json
import os
from datetime import datetime, timezone


class EventLogger:
    def __init__(self, session_id: str, log_dir: str = "logs"):
        self.session_id = session_id
        self.log_dir = log_dir
        self._trace_id: str | None = None
        os.makedirs(log_dir, exist_ok=True)
        self._file_path = os.path.join(log_dir, f"{session_id}.jsonl")

    def set_trace(self, trace_id: str) -> None:
        self._trace_id = trace_id

    def clear_trace(self) -> None:
        self._trace_id = None

    def log(self, event_type: str, payload: dict | None = None) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "trace_id": self._trace_id,
            "event": event_type,
            **(payload or {}),
        }
        with open(self._file_path, "a") as f:
            f.write(json.dumps(event) + "\n")
