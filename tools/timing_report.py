"""Summarize process timing events from a JSONL case log.

Usage:
    python -m tools.timing_report logs/case-<id>.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def summarize_timing(path: Path) -> dict[str, Any]:
    events = _load_events(path)
    summaries = [e for e in events if e.get("event") == "process_timing_summary"]
    phases = [e for e in events if e.get("event") == "process_phase_timing"]

    by_process: dict[str, dict[str, Any]] = {}
    for e in phases:
        process = str(e.get("process") or "unknown")
        phase = str(e.get("phase") or "unknown")
        bucket = by_process.setdefault(process, {
            "phase_totals": {},
            "phase_counts": {},
        })
        bucket["phase_totals"][phase] = (
            bucket["phase_totals"].get(phase, 0) + int(e.get("duration_ms") or 0)
        )
        bucket["phase_counts"][phase] = bucket["phase_counts"].get(phase, 0) + 1

    return {
        "log": str(path),
        "n_phase_events": len(phases),
        "n_summary_events": len(summaries),
        "latest_summaries": summaries[-10:],
        "by_process": by_process,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log_path", type=Path)
    parser.add_argument("--json", action="store_true", help="emit raw JSON summary")
    args = parser.parse_args()

    summary = summarize_timing(args.log_path)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return

    print(f"log: {summary['log']}")
    print(f"phase events: {summary['n_phase_events']}")
    print(f"summary events: {summary['n_summary_events']}")
    for process, data in summary["by_process"].items():
        print(f"\n[{process}]")
        totals = data["phase_totals"]
        counts = data["phase_counts"]
        for phase, total in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
            count = counts.get(phase, 0)
            avg = int(total / count) if count else 0
            print(f"  {phase}: total={total}ms count={count} avg={avg}ms")


if __name__ == "__main__":
    main()
