"""Comprehensive evaluation runner for the production agentic Q&A pipeline.

Examples:

    # Primary benchmark: ~10 questions, 10 independent runs each.
    python -m tests.test_consistency.evaluate --mode cold --k 10

    # Memory benchmark: questions execute in listed order in one warm session.
    # Repeat the sequence three times with fresh memory between sequences.
    python -m tests.test_consistency.evaluate --mode stateful --k 3

Results are written as raw JSONL, aggregate JSON, aggregate Markdown, and a
blinded human-review CSV.  No judge LLM runs on the critical path.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from tests.test_consistency.metrics import (
    aggregate_runs,
    extract_event_metrics,
    extract_trace_metrics,
    score_content,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = Path(__file__).with_name("questions.json")
RESULTS_DIR = Path(__file__).with_name("results")
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _CapturingLogger:
    """Per-session log capture that still forwards to the normal JSONL logger."""

    def __init__(self, base) -> None:
        self.base = base
        self.session_id = base.session_id
        self.records: list[dict] = []
        self._lock = threading.Lock()

    def log(self, event_type: str, payload: dict | None = None) -> None:
        row = {"event": event_type, **(payload or {})}
        with self._lock:
            self.records.append(row)
        self.base.log(event_type, payload)


def _new_session(shared: dict):
    # Importing the production harness loads the server's full runtime graph
    # (including Amem). Keep it lazy so --help and offline metric tests work in
    # a lightweight development environment.
    from tests.test_consistency.run import _HarnessSession

    values = dict(shared)
    values["logger"] = _CapturingLogger(shared["logger"])
    sess = _HarnessSession(**values)
    # TurnRunner's production CaseSession exposes this separately from logger;
    # the legacy consistency stand-in predates that attribute.
    sess.session_id = values["logger"].session_id
    return sess


def _trace_rows(turn_id: str) -> list[dict]:
    """Read committed telemetry for exactly one turn."""
    from runner.config import _NODE_TRACE_DB_PATH, _NODE_TRACE_STORE

    if _NODE_TRACE_STORE is None:
        return []
    conn = sqlite3.connect(str(_NODE_TRACE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row) for row in conn.execute(
                "SELECT * FROM node_trace WHERE turn_id = ? ORDER BY id",
                (turn_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


async def _run_turn(
    case: dict,
    run_index: int,
    *,
    mode: str,
    shared: dict,
    session,
    timeout: float,
) -> tuple[dict, Any]:
    from runner.turn.conductor import TurnRunner

    sess = session or _new_session(shared)
    event_start = len(sess.events)
    log_start = len(getattr(sess.logger, "records", []))
    turn_id = uuid.uuid4().hex[:12]
    start_dt = datetime.now()
    t0 = time.perf_counter()
    outcome, error, final_answer = "ok", "", ""

    async def pipeline() -> None:
        await TurnRunner(sess, turn_id, case["question"]).run()

    try:
        if timeout > 0:
            await asyncio.wait_for(pipeline(), timeout=timeout)
        else:
            await pipeline()
        turn_events = sess.events[event_start:]
        finals = [p for event, p in turn_events if event == "final"]
        if finals:
            final_answer = str(finals[-1].get("answer") or "")
            if final_answer.startswith("[rejected]"):
                outcome = "out_of_scope"
        else:
            errors = [p for event, p in turn_events if event == "turn_error"]
            outcome = "error"
            error = str(errors[-1].get("message") if errors else "no final event")
    except asyncio.TimeoutError:
        turn_events = sess.events[event_start:]
        outcome = "timeout"
        error = f"no response within {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001 - benchmark records failures
        turn_events = sess.events[event_start:]
        outcome = "error"
        error = f"{type(exc).__name__}: {exc}"

    row: dict[str, Any] = {
        "start_timestamp": start_dt.isoformat(timespec="seconds"),
        "mode": mode,
        "name": case["name"],
        "run_index": run_index,
        "turn_id": turn_id,
        "question": case["question"],
        "final_answer": final_answer,
        "elapsed_seconds": round(time.perf_counter() - t0, 3),
        "outcome": outcome,
        "error": error,
    }
    row.update(extract_event_metrics(turn_events))
    row.update(extract_trace_metrics(_trace_rows(turn_id)))

    # Captured event logs are useful for retry/memory debugging even though the
    # headline metrics are trace-derived. Keep only this turn's slice.
    logs = getattr(sess.logger, "records", [])[log_start:]
    row["memory_log_events"] = [
        event for event in logs
        if event.get("event") in {
            "specialist_call_dedup_hit", "active_kp_compacted",
            "case_summary_injected", "qa_cache_hit", "qa_cache_hit_near_duplicate",
        }
    ]
    row.update(score_content(row, case.get("evaluation")))
    return row, sess


async def _run_cold(args, cases, shared, persist) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    work = [
        (case, run_index)
        for case in cases
        for run_index in range(1, args.k + 1)
    ]
    done = 0
    lock = asyncio.Lock()

    async def worker(case, run_index):
        nonlocal done
        async with semaphore:
            row, _ = await _run_turn(
                case, run_index, mode="cold", shared=shared, session=None,
                timeout=args.timeout,
            )
        async with lock:
            await persist(row)
            done += 1
            print(
                f"  [{done}/{len(work)}] {row['outcome']:<12} "
                f"{row['elapsed_seconds']:>7.3f}s  {case['name']} #{run_index}"
            )
        return row

    return await asyncio.gather(*(worker(case, idx) for case, idx in work))


async def _run_stateful(args, cases, shared, persist) -> list[dict]:
    rows: list[dict] = []
    total = len(cases) * args.k
    done = 0
    for sequence_index in range(1, args.k + 1):
        sess = _new_session(shared)
        for position, case in enumerate(cases, 1):
            row, sess = await _run_turn(
                case, sequence_index, mode="stateful", shared=shared,
                session=sess, timeout=args.timeout,
            )
            row["sequence_index"] = sequence_index
            row["sequence_position"] = position
            await persist(row)
            rows.append(row)
            done += 1
            print(
                f"  [{done}/{total}] {row['outcome']:<12} "
                f"{row['elapsed_seconds']:>7.3f}s  "
                f"sequence {sequence_index}: {case['name']}"
            )
    return rows


def _markdown(summary: dict) -> str:
    def pct(value):
        return "—" if value is None else f"{100 * value:.1f}%"

    def num(value, suffix=""):
        return "—" if value is None else f"{value:.2f}{suffix}"

    lines = [
        "# Agentic Q&A evaluation",
        "",
        f"Runs: {summary['n_runs']} · Questions: {summary['n_questions']}",
        "",
        "| Mode | Question | Team exact | Tool Jaccard | Subquery similarity | "
        "Median / p95 latency | Retry | Tokens mean | LLM calls mean | "
        "QA cache hit | KP lookup hit | Provenance | Auto content |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for q in summary["questions"]:
        lat = q["latency_seconds"]
        lines.append(
            f"| {q['mode']} | {q['name']} | {pct(q['team_exact_consistency'])} | "
            f"{pct(q['tool_pairwise_jaccard'])} | "
            f"{pct(q['subquery_pairwise_similarity'])} | "
            f"{lat['median']:.2f}s / {lat['p95']:.2f}s "
            f"({lat['outlier_count']} outliers) | {pct(q['retry_rate'])} | "
            f"{q['tokens']['mean']:.0f} | {q['llm_calls']['mean']:.1f} | "
            f"{pct(q['qa_cache_hit_rate'])} | {pct(q['kp_lookup_hit_rate'])} | "
            f"{pct(q['provenance_completeness'])} | "
            f"{num(q['automated_content_score'])} |"
        )
    lines.extend([
        "",
        "Automated content is a configured structural score, not a substitute "
        "for factual review. Complete the generated blinded-review CSV for "
        "correctness and completeness.",
        "",
    ])
    return "\n".join(lines)


def _write_review_csv(path: Path, key_path: Path, rows: list[dict]) -> None:
    """Write randomized blind answers plus a separate provenance reveal key."""
    import random

    samples = list(rows)
    random.Random(20260731).shuffle(samples)
    fields = [
        "review_id", "question", "answer", "correctness_1_5",
        "completeness_1_5", "relevance_1_5", "clarity_1_5",
        "uncertainty_calibration_1_5", "scope_correct_yes_no",
        "unsupported_claims", "reviewer_notes",
    ]
    key_fields = [
        "review_id", "mode", "name", "run_index", "turn_id", "team",
        "scopes", "measured_over", "automated_content_score",
    ]
    with (
        path.open("w", newline="", encoding="utf-8") as fh,
        key_path.open("w", newline="", encoding="utf-8") as key_fh,
    ):
        writer = csv.DictWriter(fh, fieldnames=fields)
        key_writer = csv.DictWriter(key_fh, fieldnames=key_fields)
        writer.writeheader()
        key_writer.writeheader()
        for index, row in enumerate(samples, 1):
            review_id = f"R{index:04d}"
            writer.writerow({
                "review_id": review_id,
                "question": row["question"],
                "answer": row["final_answer"],
            })
            key_writer.writerow({
                "review_id": review_id,
                "mode": row.get("mode"),
                "name": row.get("name"),
                "run_index": row.get("run_index"),
                "turn_id": row.get("turn_id"),
                "team": json.dumps(row.get("team") or []),
                "scopes": json.dumps(row.get("scopes") or []),
                "measured_over": json.dumps(row.get("measured_over") or []),
                "automated_content_score": row.get("automated_content_score"),
            })


async def main_async(args) -> None:
    from tests.test_consistency.run import build_pipeline

    suite = json.loads(Path(args.suite).read_text())
    cases = suite["test_cases"][:args.limit] if args.limit else suite["test_cases"]
    backend = args.backend or os.environ.get("LLM_BACKEND") or suite.get("backend") or "openai"
    model = args.model or suite.get("model", "gpt-4.1")
    if backend == "openai" and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required for --backend openai")

    print(
        f"mode={args.mode} backend={backend} model={model} questions={len(cases)} "
        f"k={args.k} concurrency={args.concurrency}"
    )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = args.out_stem or f"evaluation_{datetime.now():%Y%m%d_%H%M%S}"
    raw_path = RESULTS_DIR / f"{stem}.jsonl"
    summary_path = RESULTS_DIR / f"{stem}_summary.json"
    markdown_path = RESULTS_DIR / f"{stem}_summary.md"
    review_path = RESULTS_DIR / f"{stem}_blind_review.csv"
    review_key_path = RESULTS_DIR / f"{stem}_review_key.csv"
    raw_path.write_text("", encoding="utf-8")
    persist_lock = asyncio.Lock()

    async def persist(row: dict) -> None:
        # Append as each expensive run completes so a later timeout/process
        # interruption does not discard earlier results.
        async with persist_lock:
            with raw_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")

    shared = build_pipeline(
        suite, backend=backend, model=model,
        concurrency_cap=int(suite.get("concurrency_cap", 12)),
    )
    if args.mode == "cold":
        rows = await _run_cold(args, cases, shared, persist)
    elif args.mode == "stateful":
        rows = await _run_stateful(args, cases, shared, persist)
    else:
        cold = await _run_cold(args, cases, shared, persist)
        stateful = await _run_stateful(args, cases, shared, persist)
        rows = cold + stateful

    summary = aggregate_runs(rows)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(summary), encoding="utf-8")
    _write_review_csv(review_path, review_key_path, rows)
    print(f"Raw results : {raw_path}")
    print(f"Summary     : {markdown_path}")
    print(f"Blind review: {review_path}")
    print(f"Review key  : {review_key_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--mode", choices=["cold", "stateful", "both"], default="cold")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--backend", choices=["openai", "safechain"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out-stem", default=None)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
