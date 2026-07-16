"""Specialist dispatch + tool-usage checks for extraction questions.

Motivating question (private-env regression testing):

    "extract and summarize the abnormal transactions when TSR was reacting in 2025"

The RIGHT behaviour is:
  1. the orchestrator DISPATCHES a domain specialist (does not answer directly);
  2. that specialist calls **transaction_detail** on the model table filtered by
     the TSR threshold AND the 2025 window — which returns the joined per-txn
     records (time + merchant + amount + TSR + CDSS + top/bottom drivers).

This file has two independent parts:

  • PART 1 — TOOL LAYER (deterministic, runs in dev now). Proves the tool
    produces the correct grounded extraction for this question against the real
    case data. If a specialist calls it, the answer is grounded. Skips cleanly
    when the real case CSVs aren't present.

  • PART 2 — DISPATCH / TOOL-USAGE FROM A REAL RUN (run in the PRIVATE env).
    `summarize_tool_usage()` scans a run's JSONL log(s) and reports whether a
    specialist was dispatched and whether transaction_detail was used (and how
    it was filtered). The env-gated test asserts it. Usage:

        # 1. (optional) start from a clean log dir so only this turn's calls
        #    are present, then ask the question in the app / private env.
        # 2. point the test at the server log written during that turn:
        TOOL_USAGE_LOG="logs/*.jsonl" \
            python -m pytest tests/test_specialist_tool_usage.py -k from_log -s
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import pytest

# ── shared: real-case fixture ────────────────────────────────────────────────

_REAL_ROOT = Path("data_tables/real")
_REAL_CASE = "366132845011"
_TSR_THRESHOLD = "20"          # tot_struct_risk_score risky ≥ 20 (from schema)
_YEAR_START = "2025-01-01"

_real_available = (_REAL_ROOT / _REAL_CASE / "modelling_data_transaction.csv").exists()
_needs_real = pytest.mark.skipif(
    not _real_available,
    reason=f"real case data not present at {_REAL_ROOT / _REAL_CASE}",
)


@pytest.fixture()
def real_tools():
    """Bind data_tools to the real case gateway + catalog (env-agnostic layer)."""
    from datalayer.gateway import LocalDataGateway
    from datalayer.catalog import DataCatalog
    import tools.data_tools as dt

    gw = LocalDataGateway.from_case_folders(str(_REAL_ROOT))
    gw.set_case(_REAL_CASE)
    dt.init_tools(gw, DataCatalog())
    try:
        yield dt
    finally:
        dt._gateway = None
        dt._catalog = None


# ── PART 1 — tool layer (deterministic) ──────────────────────────────────────

def _tsr_reacted_2025_filters() -> str:
    return json.dumps([
        {"column": "tot_struct_risk_score", "op": "gte", "value": _TSR_THRESHOLD},
        {"column": "trans_dt", "op": "gte", "value": _YEAR_START},
    ])


@_needs_real
def test_transaction_detail_extracts_tsr_reacted_2025(real_tools):
    """The canonical call for this question returns a real, joined extraction —
    not empty, not scores-only. This is what the specialist SHOULD produce."""
    out = json.loads(real_tools._transaction_detail_impl(
        base_table="model_scores_transaction",
        filters=_tsr_reacted_2025_filters(),
        sort_by="", sort_desc=False, limit=30, columns="",
        filter_column="", filter_value="", filter_op="eq", timestamps=""))
    # A meaningful number of transactions crossed the TSR threshold in 2025.
    assert out["transactions_selected"] >= 50
    rows = out["rows"]
    assert rows, "expected joined transaction rows"
    # Coverage is reported so declines aren't mislabeled as a broken join.
    assert "merchant_amount_coverage" in out
    # Some rows carry the settled-spend side (merchant + amount).
    assert any(r.get("Merchant Name") and r.get("Amount") for r in rows)


@_needs_real
def test_transaction_detail_row_carries_scores_and_drivers(real_tools):
    """Each row is the JOINED record: TSR + CDSS + top/bottom drivers for BOTH
    scores — the summary must speak to spend AND risk, so all must be present."""
    out = json.loads(real_tools._transaction_detail_impl(
        base_table="model_scores_transaction",
        filters=_tsr_reacted_2025_filters(),
        sort_by="", sort_desc=False, limit=10, columns="",
        filter_column="", filter_value="", filter_op="eq", timestamps=""))
    keys = set(out["rows"][0].keys())
    assert {"tot_struct_risk_score", "credit_loss_prob"} <= keys      # TSR + CDSS
    assert {"top_tsr1", "bottom_tsr1"} <= keys                         # TSR drivers (both dirs)
    assert {"top_cdss1", "bottom_cdss1"} <= keys                       # CDSS drivers (both dirs)


@_needs_real
def test_scores_only_query_is_the_wrong_tool(real_tools):
    """Illustrates WHY transaction_detail matters: querying the model table alone
    yields scores but NO merchant/amount — an extraction answered from this would
    be missing half the picture. (The specialist should prefer transaction_detail.)"""
    out = json.loads(real_tools._query_table_impl(
        "model_scores_transaction",
        filters=_tsr_reacted_2025_filters(),
        columns="trans_dt,tot_struct_risk_score,credit_loss_prob", limit=10))
    row = out["rows"][0]
    assert "Merchant Name" not in row and "Amount" not in row   # no spend side


# ── PART 2 — dispatch / tool-usage from a real run's log ──────────────────────

_DATA_TOOLS = {
    "query_table", "batch_query_table", "transaction_detail", "join_table",
    "aggregate_column", "batch_aggregate", "summarize_trend",
    "batch_summarize_trend", "summarize_by_group", "get_table_schema",
    "list_available_tables",
}


def summarize_tool_usage(log_glob: str) -> dict:
    """Scan run JSONL log(s) and summarize dispatch + tool usage.

    Returns:
        {
          "files": [...],
          "n_tool_calls": int,
          "data_tools_used": sorted[str],       # which data tools any specialist called
          "specialist_dispatched": bool,        # any data tool call → a specialist ran
          "transaction_detail_used": bool,
          "transaction_detail_calls": [args, ...],
          "specs_unparseable": int,             # truncated-specs anti-fabrication fires
          "dispatch_skips": int,                # orchestrator answered with 0 tools
        }
    """
    files = sorted(glob.glob(log_glob))
    data_used: set[str] = set()
    td_calls: list[dict] = []
    n_calls = 0
    specs_unparseable = 0
    # Enforcement / retry telemetry — tells us whether the dispatch-skip
    # enforcement + escalating retries are actually RUNNING (present in the
    # server process), vs a stale build that never fires them.
    retry_no_tools = 0          # orchestrator_retry_no_tools (0-tools retry)
    dispatch_skip_retries = 0   # orchestrator_retry reason=dispatch_skip (my fix)
    mbe_retries = 0             # orchestrator_retry reason=model_behavior_error
    no_tool_calls_final = 0     # orchestrator_no_tool_calls (released 0-tools answer)
    max_attempt = 0
    for f in files:
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                e = r.get("event")
                if e == "tool_call":
                    tool = r.get("tool")
                    n_calls += 1
                    if tool in _DATA_TOOLS:
                        data_used.add(tool)
                    if tool == "transaction_detail":
                        td_calls.append(r.get("args", {}))
                elif e == "tool_result" and r.get("reason") == "specs_unparseable":
                    specs_unparseable += 1
                elif e == "orchestrator_retry_no_tools":
                    retry_no_tools += 1
                    max_attempt = max(max_attempt, r.get("attempt", 0))
                elif e == "orchestrator_retry":
                    reason = r.get("reason")
                    if reason == "dispatch_skip":
                        dispatch_skip_retries += 1
                    elif reason == "model_behavior_error":
                        mbe_retries += 1
                    max_attempt = max(max_attempt, r.get("attempt", 0))
                elif e == "orchestrator_no_tool_calls":
                    no_tool_calls_final += 1
    return {
        "files": files,
        "n_tool_calls": n_calls,
        "data_tools_used": sorted(data_used),
        "specialist_dispatched": bool(data_used),
        "transaction_detail_used": bool(td_calls),
        "transaction_detail_calls": td_calls,
        "specs_unparseable": specs_unparseable,
        # enforcement telemetry (0 across the board = enforcement never ran →
        # the server process is likely on a STALE build; restart it after pull):
        "enforcement_retries_fired": retry_no_tools + dispatch_skip_retries + mbe_retries,
        "retry_no_tools": retry_no_tools,
        "dispatch_skip_retries": dispatch_skip_retries,
        "mbe_retries": mbe_retries,
        "max_retry_attempt": max_attempt,
        "released_ungrounded_answer": no_tool_calls_final,
    }


def _td_filters_look_right(args: dict) -> bool:
    """True if a transaction_detail call filtered by the TSR score and a 2025
    window (either via `filters` or the single filter_column path)."""
    blob = json.dumps(args)
    has_tsr = "tot_struct_risk_score" in blob
    has_2025 = "2025" in blob
    return has_tsr and has_2025


@pytest.mark.skipif(not os.environ.get("TOOL_USAGE_LOG"),
                    reason="set TOOL_USAGE_LOG=<glob> to a run's JSONL log(s)")
def test_specialist_dispatch_and_tool_usage_from_log():
    """Point at a run's log (private env) and assert the specialist dispatched +
    used transaction_detail correctly for the TSR-reacted-2025 extraction.

        TOOL_USAGE_LOG="logs/*.jsonl" python -m pytest \
            tests/test_specialist_tool_usage.py -k from_log -s
    """
    summary = summarize_tool_usage(os.environ["TOOL_USAGE_LOG"])
    print("\n=== tool-usage summary ===")
    print(json.dumps({k: v for k, v in summary.items()
                      if k != "transaction_detail_calls"}, indent=2))
    for a in summary["transaction_detail_calls"]:
        print("  transaction_detail args:", json.dumps(a))

    assert summary["specialist_dispatched"], (
        "no data-tool calls found — the orchestrator likely answered WITHOUT "
        "dispatching a specialist (dispatch skip).")
    assert summary["transaction_detail_used"], (
        f"a specialist ran but did NOT call transaction_detail; tools used: "
        f"{summary['data_tools_used']}. For an extraction+scores question it "
        f"should use transaction_detail to get the joined records.")
    assert any(_td_filters_look_right(a) for a in summary["transaction_detail_calls"]), (
        "transaction_detail was called but not filtered by the TSR score AND a "
        "2025 window — check the specialist's filter construction.")
