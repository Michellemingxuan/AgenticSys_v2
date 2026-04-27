"""Non-interactive demos of the sync CLI's review flow.

Each test feeds a scripted ``input()`` sequence into the real interactive
helpers and prints what a human reviewer would see. Run with::

    pytest tests/test_sync_interactive_demo.py -s -v

The ``-s`` flag is essential — without it pytest swallows the printed
demo output. Each test asserts the bare minimum (writes happened) so
they double as regressions for the UX shape.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from datalayer import adapter
from datalayer import sync as sync_mod
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from agents.data_manager_agent import DataManagerAgent


class _NullLogger:
    def log(self, *a, **k):
        pass


class _StubAgent(DataManagerAgent):
    """DataManagerAgent with canned LLM drafts (no API call)."""

    async def draft_description(self, table, column, samples, sibling_columns=None, dtype=None):
        return f"<llm-draft> '{column}' in '{table}' looks like a {dtype}"

    async def draft_table_description(self, table, column_names):
        return f"<llm-draft> '{table}' contains {len(column_names)} columns about a TBD entity"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Build a real catalog + real gateway + stub agent.

    Disables rich.Console so output goes through plain ``print()`` — that
    way pytest's ``capsys`` captures it cleanly without ANSI escapes.
    """
    monkeypatch.setattr(sync_mod, "_CONSOLE", None)

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "transactions.yaml").write_text("""\
table: transactions
description: "Transaction data"
columns:
  amount:
    dtype: float
    description: "Amount in USD"
    aliases: [trans_amt]
  transaction_date:
    dtype: date
    description: "Transaction date"
    aliases: []
""")
    (profile_dir / "bureau.yaml").write_text("""\
table: bureau
description: "Credit bureau data"
columns:
  fico_score:
    dtype: int
    description: "FICO score"
    aliases: ["FICO Score"]
  dpd_status:
    dtype: str
    description: "Days-past-due status"
    aliases: []
""")

    real = tmp_path / "real"
    real.mkdir()
    case_dir = real / "CASE_DEMO"
    case_dir.mkdir()
    (case_dir / "transactions.csv").write_text(
        "trans_amt,transaction_dt,brand_new\n"
        "12.5,2025-01-01,foo\n30.0,2025-02-01,bar\n"
    )
    (case_dir / "bureau.csv").write_text(
        "FICO Score,risky_flag\n720,0\n680,1\n"
    )
    (case_dir / "completely_new_table.csv").write_text(
        "foo,bar\n1,a\n2,b\n"
    )

    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = LocalDataGateway.from_case_folders(str(real))
    agent = _StubAgent(gateway=gateway, catalog=catalog, llm=None, logger=_NullLogger())
    return agent, catalog, gateway


def _reconcile_and_aggregate(agent, gateway):
    canonical = {t: agent.catalog._profiles[t]["columns"] for t in agent.catalog.list_tables()}
    diffs = []
    for cid in gateway.list_case_ids():
        diff = adapter.reconcile_case(gateway, canonical, cid)
        adapter.apply_diff(diff, agent.catalog)
        diffs.append(diff)
    return adapter.aggregate_diffs(diffs)


async def _run_async_phases(agent, agg, gateway, *, accept_drafts):
    await sync_mod._verify_new_columns(agent, agg, gateway, accept_drafts=accept_drafts)
    await sync_mod._verify_new_tables(agent, agg, gateway, accept_drafts=accept_drafts)


def _run_interactive(agent, agg, gateway, scripted, *, auto_threshold=0.0, accept_drafts=False):
    """Feed scripted inputs; any input beyond the script defaults to ENTER.

    ENTER triggers each prompt's default action (accept top / accept draft),
    which models how a real user would breeze through routine confirmations.
    """
    inputs = iter(scripted)

    def _next(*_a, **_k):
        try:
            return next(inputs)
        except StopIteration:
            return ""  # = ENTER = default action

    with patch("builtins.input", side_effect=_next):
        sync_mod._resolve_ambiguous(agent, agg, gateway, auto_threshold=auto_threshold)
        asyncio.run(_run_async_phases(agent, agg, gateway, accept_drafts=accept_drafts))


def _print_block(label: str, text: str) -> None:
    bar = "═" * 70
    print(f"\n{bar}\n{label}\n{bar}\n{text}\n{bar}")


# ── Demo 1 — fully autonomous: --auto-threshold 0.95 + --accept-drafts ─

def test_demo_one_shot_autoaccept(env, capsys):
    """Fastest path: auto-threshold catches confident ambiguous, all drafts auto-accepted."""
    agent, catalog, gateway = env
    agg = _reconcile_and_aggregate(agent, gateway)

    # Zero scripted inputs needed — accept_drafts=True skips per-table menu.
    _run_interactive(agent, agg, gateway, scripted=[],
                     auto_threshold=0.85, accept_drafts=True)

    out = capsys.readouterr().out
    _print_block("DEMO 1 — `--auto-threshold 0.85 --accept-drafts` (zero prompts)", out)

    # Confirm writes happened
    assert "auto-accepted" in out.lower() or "drafts auto-accepted" in out.lower()


# ── Demo 2 — batch shortcuts: ENTER=a at every per-table menu ─────────

def test_demo_per_table_batch_accept(env, capsys):
    agent, catalog, gateway = env
    agg = _reconcile_and_aggregate(agent, gateway)

    # Per-table menus only:
    #   ambiguous: 1 table (transactions) → 'a'
    #   new cols : 2 tables (transactions, bureau) → 'a', 'a'
    #   new tabs : 1 (completely_new_table) → individual ENTER (default 'a')
    scripted = ["a", "a", "a", ""]
    _run_interactive(agent, agg, gateway, scripted=scripted)

    out = capsys.readouterr().out
    _print_block("DEMO 2 — per-table batch ('a' at each menu)", out)

    assert "aliased to top candidate" in out.lower()
    assert "drafts accepted" in out.lower()


# ── Demo 3 — review each entry, ENTER = accept default ────────────────

def test_demo_review_each_with_enter_defaults(env, capsys):
    agent, catalog, gateway = env
    agg = _reconcile_and_aggregate(agent, gateway)

    # Ambiguous: 1 table → 'r' (review), then per-entry: '' (ENTER → top).
    # New cols: transactions table 'r', then 1 entry '' (ENTER → accept).
    # New cols: bureau table 'r', then 1 entry '' (ENTER → accept).
    # New tables: 1 entry '' (ENTER → accept).
    scripted = ["r", "", "r", "", "r", "", ""]
    _run_interactive(agent, agg, gateway, scripted=scripted)

    out = capsys.readouterr().out
    _print_block("DEMO 3 — review each, ENTER=accept default", out)

    assert "samples:" in out.lower()
    assert "<llm-draft>" in out


# ── Demo 4 — review-each with one edit and one skip ──────────────────

def test_demo_mixed_actions(env, capsys):
    """Show the 'edit' branch and the 'skip table' branch.

    Inputs scripted explicitly; everything else falls back to ENTER (=accept).
    """
    agent, catalog, gateway = env
    agg = _reconcile_and_aggregate(agent, gateway)

    # First menu = ambiguous transactions table → 's' (skip the whole table).
    # Second menu = new cols bureau table → 'r' (review).
    # Third prompt = bureau.risky_flag per-entry → 'e' then text (edit it).
    # Everything after that defaults to ENTER (=accept).
    scripted = ["s", "r", "e", "edited by reviewer"]
    _run_interactive(agent, agg, gateway, scripted=scripted)

    out = capsys.readouterr().out
    _print_block("DEMO 4 — skip ambiguous table, edit one draft, ENTER for the rest", out)

    assert "verified with edit" in out.lower()
