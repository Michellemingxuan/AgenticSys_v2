"""Lightweight process timing helper for JSONL event logs."""
from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Any, Iterator


class ProcessTimer:
    """Collect named phase timings and write them through EventLogger.

    The helper is intentionally small: it has no background state, no global
    registry, and no dependency on Flask or the Agents SDK. Callers can emit
    individual phase events as work completes, then a summary event at the end
    of the process.
    """

    def __init__(self, logger: Any, process: str, **base_payload: Any) -> None:
        self.logger = logger
        self.process = process
        self.base_payload = dict(base_payload)
        self.started_at = perf_counter()
        self.phases: list[dict[str, Any]] = []

    @contextmanager
    def phase(self, name: str, **payload: Any) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            self.record(name, int((perf_counter() - start) * 1000), **payload)

    def record(self, name: str, duration_ms: int, **payload: Any) -> None:
        entry = {
            "phase": name,
            "duration_ms": int(duration_ms),
            **payload,
        }
        self.phases.append(entry)
        self._log("process_phase_timing", entry)

    def summary(self, **payload: Any) -> dict[str, Any]:
        total_ms = int((perf_counter() - self.started_at) * 1000)
        phase_totals: dict[str, int] = {}
        for p in self.phases:
            phase = str(p.get("phase") or "unknown")
            phase_totals[phase] = phase_totals.get(phase, 0) + int(
                p.get("duration_ms") or 0
            )
        out = {
            "total_ms": total_ms,
            "phase_totals": phase_totals,
            "n_phases": len(self.phases),
            **payload,
        }
        self._log("process_timing_summary", out)
        return out

    def _log(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.logger is None:
            return
        try:
            self.logger.log(event_type, {
                "process": self.process,
                **self.base_payload,
                **payload,
            })
        except Exception:
            pass
