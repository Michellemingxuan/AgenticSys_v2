"""Consistency / heavy-traffic test harness.

Runs every seed question in a suite (``questions.json``) **k** times through the
real reviewer pipeline (screen → orchestrator → format), captures the final
answer and wall-clock latency of each response, and appends one row per
response to a CSV. Built to exercise the LLM (gpt-4.1, 2024-06-01) under load —
e.g. to check answer consistency and response-time spread when the private env
is busy.

It mirrors the proven wiring in ``notebooks/run_question_suite.py`` (the same
``questions.json`` schema), but:

  * only repeats each *seed* question (follow-up chains are skipped), and
  * adds a ``--concurrency`` knob and per-response timing → CSV.

The agent architecture is identical in both environments — only the LLM
transport differs (``llm.factory.build_session_clients(backend=...)``). So this
harness runs unchanged in either env; set the backend per the suite / CLI:
``openai`` in dev, ``safechain`` in the private/prod env (``--backend
safechain`` or ``LLM_BACKEND=safechain``).

SafeChain note: a hung ``chain.invoke`` occupies a worker thread that cannot be
cancelled, so under load a single turn can wedge. Two defences here:
  * ``--timeout`` wraps each response in ``asyncio.wait_for`` — on expiry the
    row is recorded as ``timeout`` and the run continues (the orphaned worker
    thread may linger until SafeChain returns; that is unavoidable).
  * keep ``--concurrency`` modest: each in-flight request fans out to several
    specialist calls, each taking a slot from the SafeChain pool
    (``SAFECHAIN_THREAD_POOL``, default 32). Over-subscribing it is the
    documented "stuck at team construction" hang.

CSV columns (one row per response):

    start_timestamp   ISO-8601 local time the request was dispatched
    name              test-case name from the suite
    run_index         1..k — which repeat this is
    question          the question asked
    final_answer      the formatted answer the reviewer would see
                      (or "[rejected] …" for the safety/scope screen)
    elapsed_seconds   wall-clock seconds for the full response
    outcome           ok | out_of_scope | timeout | error
    error             timeout/exception text, else empty

Run (dev):

    python -m tests.test_consistency.run --k 5 --concurrency 1

Heavy-traffic load in the private env:

    LLM_BACKEND=safechain python -m tests.test_consistency.run \
        --k 5 --concurrency 4 --backend safechain --timeout 600
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Pin CWD + sys.path so relative paths (config/, data_tables/, reports/, logs/)
# resolve against the project root regardless of where this is launched from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before importing anything that needs OPENAI_API_KEY.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agent_factories.chat_agent import ChatAgent
from agent_factories.data_manager_agent import DataManagerAgent
from agent_factories.helper_tools import build_helper_tools
from config.pillar_loader import PillarLoader
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from llm.factory import FirewalledChatShim, build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from orchestrator.orchestrator import Orchestrator
from tools.data_tools import init_tools


DEFAULT_SUITE = Path(__file__).with_name("questions.json")
RESULTS_DIR = Path(__file__).with_name("results")

CSV_FIELDS = [
    "start_timestamp",
    "name",
    "run_index",
    "question",
    "final_answer",
    "elapsed_seconds",
    "outcome",
    "error",
]


# ── Pipeline construction (mirrors notebooks/run_question_suite.main setup) ──

def build_pipeline(suite: dict, *, backend: str, model: str, concurrency_cap: int):
    """Wire up the full reviewer pipeline once and return the shared pieces.

    Returns ``(chat_agent, orch, case_folder)``. ``orch.run`` builds a fresh
    ``AppContext`` per call, so the returned objects are safe to drive
    concurrently from multiple in-flight requests.
    """
    case_id = suite["case_id"]
    pillar_name = suite["pillar"]
    session_id = suite.get("session_id", "consistency-suite")

    logger = EventLogger(session_id=session_id, log_dir=str(PROJECT_ROOT / "logs"))
    firewall = FirewallStack(logger=logger, max_retries=2, concurrency_cap=concurrency_cap)
    clients = build_session_clients(firewall, model_name=model, backend=backend)
    chat_llm = FirewalledChatShim(clients)

    gw = LocalDataGateway.from_case_folders(str(PROJECT_ROOT / "data_tables" / "real"))
    catalog = DataCatalog(profile_dir=str(PROJECT_ROOT / "config" / "data_profiles"))
    assert catalog.list_tables(), "DataCatalog loaded 0 profiles — check path."
    init_tools(gw, catalog, logger=logger)
    gw.set_case(case_id)

    # Sync the catalog with this case's real data BEFORE building agents so
    # column aliases / value drift are resolved (matches the question suite).
    data_mgr = DataManagerAgent(gateway=gw, catalog=catalog, llm=None, logger=logger)
    diff = data_mgr.sync_catalog(case_id)
    print(
        f"sync_catalog: auto={len(diff.auto_aliased)} drift={len(diff.value_drift)} "
        f"ambig={len(diff.ambiguous)} new_cols={len(diff.new)} new_tables={len(diff.new_tables)}"
    )

    pillar_yaml = PillarLoader().load(pillar_name) or {}
    chat_agent = ChatAgent(chat_llm, logger, tools=build_helper_tools())
    orch = Orchestrator(
        llm=None,
        logger=logger,
        registry=None,
        pillar=pillar_name,
        pillar_config=pillar_yaml,
        catalog=catalog,
        gateway=gw,
        clients=clients,
    )
    case_folder = PROJECT_ROOT / "reports" / case_id
    return chat_agent, orch, case_folder


# ── One response ────────────────────────────────────────────────────────────

async def run_once(
    name, run_index, question, *, chat_agent, orch, case_folder, timeout=None
) -> dict:
    """Drive one question through the reviewer path, timing the whole response.

    Mirrors ``main.py:_screen_and_run`` — screen (redact + scope) → orchestrator
    → format — so the captured ``final_answer`` is exactly what a reviewer sees.

    ``timeout`` (seconds, or None/0 to disable) caps the whole response via
    ``asyncio.wait_for``. On expiry the row is marked ``timeout`` and the run
    continues. Under SafeChain a blocking ``chain.invoke`` can't be cancelled,
    so the underlying worker thread may stay occupied past this deadline — the
    cap protects the *test driver* from wedging, not the worker pool.
    """
    start_dt = datetime.now()
    t0 = time.perf_counter()
    outcome = "ok"
    error = ""
    final_answer = ""

    async def _pipeline():
        verdict = await chat_agent.screen(question)
        if not verdict.passed:
            return "out_of_scope", f"[rejected] {verdict.reason}"
        final = await orch.run(verdict.redacted_question, case_folder)
        return "ok", chat_agent.format(final)

    try:
        if timeout and timeout > 0:
            outcome, final_answer = await asyncio.wait_for(_pipeline(), timeout=timeout)
        else:
            outcome, final_answer = await _pipeline()
    except asyncio.TimeoutError:
        outcome = "timeout"
        error = (
            f"no response within {timeout:.0f}s "
            "(SafeChain worker thread may still be occupied)"
        )
    except Exception as exc:  # noqa: BLE001 — load test must record, not crash
        outcome = "error"
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - t0

    return {
        "start_timestamp": start_dt.isoformat(timespec="seconds"),
        "name": name,
        "run_index": run_index,
        "question": question,
        "final_answer": final_answer,
        "elapsed_seconds": round(elapsed, 3),
        "outcome": outcome,
        "error": error,
    }


# ── Top-level orchestration ─────────────────────────────────────────────────

async def main_async(args) -> None:
    suite = json.loads(Path(args.suite).read_text())

    # Backend precedence: explicit --backend > LLM_BACKEND env > suite default >
    # "openai". The suite's "backend" is a committed default; the *environment*
    # (which env we're deployed in) must win over it so the same questions.json
    # runs as openai in dev and safechain in private without editing the file.
    backend = (
        args.backend
        or os.environ.get("LLM_BACKEND")
        or suite.get("backend")
        or "openai"
    )
    model = args.model or suite.get("model", "gpt-4.1")
    concurrency_cap = int(suite.get("concurrency_cap", 12))

    # Fail fast with an actionable message instead of the opaque
    # "openai.OpenAIError: api_key must be set" that AsyncOpenAI() raises when
    # the openai backend is selected without a key (the usual symptom of
    # running in the private env without selecting safechain).
    if backend == "openai" and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "Backend resolved to 'openai' but OPENAI_API_KEY is not set.\n"
            "  • In the private/prod env, select SafeChain: pass --backend "
            "safechain or export LLM_BACKEND=safechain.\n"
            "  • In dev, set OPENAI_API_KEY (e.g. in .env).\n"
            f"(resolved from: --backend={args.backend!r}, "
            f"LLM_BACKEND={os.environ.get('LLM_BACKEND')!r}, "
            f"suite.backend={suite.get('backend')!r})"
        )

    cases = suite["test_cases"]
    if args.limit:
        cases = cases[: args.limit]

    print(f"PROJECT_ROOT      : {PROJECT_ROOT}")
    print(
        f"backend={backend}  model={model}  case_id={suite['case_id']}  "
        f"k={args.k}  request_concurrency={args.concurrency}  "
        f"firewall_concurrency_cap={concurrency_cap}  "
        f"timeout={args.timeout or 'off'}  questions={len(cases)}"
    )
    if backend == "safechain":
        sc_pool = int(os.environ.get("SAFECHAIN_THREAD_POOL", "32"))
        sc_to = os.environ.get("SAFECHAIN_CALL_TIMEOUT_S", "180")
        print(
            f"SafeChain         : thread_pool={sc_pool}  call_timeout_s={sc_to}  "
            "(each in-flight request fans out to several specialist calls — keep "
            "--concurrency well under the pool to avoid the 'stuck at team "
            "construction' hang)"
        )
        if args.concurrency > max(1, sc_pool // 4):
            print(
                f"  WARNING: --concurrency {args.concurrency} is high for a "
                f"{sc_pool}-thread SafeChain pool; specialist fan-out may exhaust "
                "it and stall. Consider a lower value or raise SAFECHAIN_THREAD_POOL."
            )
    else:
        print(f"OPENAI_API_KEY set: {bool(os.environ.get('OPENAI_API_KEY'))}")

    chat_agent, orch, case_folder = build_pipeline(
        suite, backend=backend, model=model, concurrency_cap=concurrency_cap
    )

    # Build the flat work list: each seed question repeated k times.
    work = [
        (case["name"], r, case["question"])
        for case in cases
        for r in range(1, args.k + 1)
    ]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else RESULTS_DIR / f"consistency_{stamp}.csv"

    # Open the CSV up front and append each row as it completes (a lock keeps
    # concurrent writers from interleaving). Partial results survive a mid-run
    # failure — important when the whole point is to stress a flaky env.
    write_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = 0
    total = len(work)

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        fh.flush()

        async def worker(name, run_index, question):
            nonlocal done
            async with sem:
                row = await run_once(
                    name, run_index, question,
                    chat_agent=chat_agent, orch=orch, case_folder=case_folder,
                    timeout=args.timeout,
                )
            async with write_lock:
                writer.writerow(row)
                fh.flush()
                done += 1
                print(
                    f"  [{done}/{total}] {row['outcome']:<12} "
                    f"{row['elapsed_seconds']:>7.3f}s  {name} #{run_index}"
                )
            return row

        rows = await asyncio.gather(
            *(worker(n, r, q) for (n, r, q) in work),
            return_exceptions=False,
        )

    # ── Summary ─────────────────────────────────────────────────────────────
    by_outcome: dict[str, int] = {}
    for row in rows:
        by_outcome[row["outcome"]] = by_outcome.get(row["outcome"], 0) + 1
    times = [row["elapsed_seconds"] for row in rows]
    print()
    print("Done.")
    print(f"  responses : {len(rows)}  ({by_outcome})")
    if times:
        ordered = sorted(times)
        p50 = ordered[len(ordered) // 2]
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(
            f"  latency   : min={ordered[0]:.3f}s  p50={p50:.3f}s  "
            f"p95={p95:.3f}s  max={ordered[-1]:.3f}s"
        )
    print(f"  CSV       : {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repeat each suite question k times through the reviewer "
                    "pipeline and record per-response answer + latency to CSV."
    )
    parser.add_argument("--suite", type=str, default=str(DEFAULT_SUITE),
                        help=f"Path to the questions JSON (default: {DEFAULT_SUITE}).")
    parser.add_argument("--k", type=int, default=3,
                        help="How many times to repeat each question (default: 3).")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="In-flight requests. 1 = sequential clean timing; "
                             ">1 = concurrent load generation (default: 1).")
    parser.add_argument("--backend", choices=["openai", "safechain"], default=None,
                        help="Override the suite's LLM backend. Use 'safechain' "
                             "in the private/prod env.")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="Per-response wall-clock cap in seconds; on expiry the "
                             "row is marked 'timeout' and the run continues. 0 "
                             "disables. Default 600 (> SafeChain's internal "
                             "TURN_WALL_CLOCK_S=360 + screen, so it only catches "
                             "true hangs).")
    parser.add_argument("--model", type=str, default=None,
                        help="Override the suite's model (default from suite: gpt-4.1).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only run the first N questions of the suite.")
    parser.add_argument("--out", type=str, default=None,
                        help="CSV output path (default: results/consistency_<ts>.csv).")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
