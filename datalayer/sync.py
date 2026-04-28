"""CLI: reconcile real-data CSVs against the canonical YAML catalog.

Usage:
    python -m datalayer.sync                          # all real cases, full interactive
    python -m datalayer.sync --case-id X              # one case
    python -m datalayer.sync --dry-run                # diff only, no writes
    python -m datalayer.sync --no-llm                 # skip LLM drafting
    python -m datalayer.sync --non-interactive        # diff only, no prompts
    python -m datalayer.sync --include-simulated      # also reconcile simulated
    python -m datalayer.sync --auto-threshold 0.95    # auto-accept ambiguous w/ top ratio ≥ X
    python -m datalayer.sync --accept-drafts          # accept LLM drafts without per-col prompt

Interactive UX:
  Entries are grouped by table. Each table opens with a one-key menu:
    [a]ccept all top candidates / drafts   [r]eview each   [s]kip table
  Inside review: ENTER = accept top / accept draft.
  Writes are eager and idempotent — Ctrl+C is safe; re-run picks up the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import uuid
from pathlib import Path

from datalayer import adapter
from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from logger.event_logger import EventLogger

try:
    from rich.console import Console
    from rich.rule import Rule
    from rich.text import Text
    _CONSOLE: "Console | None" = Console()
except ImportError:  # pragma: no cover — rich is in requirements
    _CONSOLE = None


def _say(msg: str = "", *, style: str = "") -> None:
    """Print with rich styling when available, plain otherwise."""
    if _CONSOLE is not None and style:
        _CONSOLE.print(msg, style=style)
    elif _CONSOLE is not None:
        _CONSOLE.print(msg)
    else:
        print(msg)


def _rule(label: str = "", *, style: str = "cyan") -> None:
    if _CONSOLE is not None:
        _CONSOLE.print(Rule(label, style=style))
    else:
        print(f"\n── {label} ──")


def _ask(prompt: str, default: str = "") -> str:
    """Prompt with optional default (shown in brackets). Empty input → default.

    EOF (e.g. piped/non-interactive shell) is treated as the default so
    a ``--auto-threshold ... --accept-drafts`` run can finish without TTY.
    """
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return raw or default


_REPO_ROOT = Path(__file__).parent.parent
_REAL_DIR = _REPO_ROOT / "data_tables" / "real"
_SIM_DIR = _REPO_ROOT / "data_tables" / "simulated"


# ── Loading ──────────────────────────────────────────────────────────────

def _load_gateway(
    sources: list[Path],
) -> tuple[LocalDataGateway, list[str], dict[str, dict[str, list[str]]]]:
    """Build a gateway by walking the given source folders.

    Returns ``(gateway, xlsx_warnings, headers_by_case)``. The headers map
    ``{case_id: {table: [col_names]}}`` is captured directly from CSV
    headers so the audit can be schema-level even when a table is
    physically empty (header-only CSV).

    Applies the same post-load transforms as
    :meth:`LocalDataGateway.from_case_folders` (currently: rbind
    ``payments_success`` + ``payments_returns`` into ``payments``, with
    a matching synthetic header for the merged table).
    """
    case_data: dict[str, dict[str, list[dict]]] = {}
    headers_by_case: dict[str, dict[str, list[str]]] = {}
    xlsx_warnings: list[str] = []
    for src in sources:
        for case_dir in sorted(src.iterdir()):
            if not case_dir.is_dir():
                continue
            cid = case_dir.name
            tables = case_data.setdefault(cid, {})
            headers = headers_by_case.setdefault(cid, {})
            for f in sorted(case_dir.iterdir()):
                if f.suffix == ".csv":
                    # utf-8-sig handles Excel BOM-prefixed CSVs cleanly.
                    with open(f, encoding="utf-8-sig") as fh:
                        rdr = csv.DictReader(fh)
                        headers[f.stem] = list(rdr.fieldnames or [])
                        tables[f.stem] = list(rdr)
                elif f.suffix in (".xlsx", ".xls"):
                    xlsx_warnings.append(f"{cid}/{f.name}")
            LocalDataGateway._rbind_payments(tables)
            # Mirror the rbind in the headers map so the audit sees `payments`.
            if "payments" in tables and "payments" not in headers:
                base = headers.pop("payments_success", None) or headers.pop("payments_returns", None) or []
                headers.pop("payments_success", None)
                headers.pop("payments_returns", None)
                headers["payments"] = list(base) + ["payment_status"]
    return LocalDataGateway(case_data=case_data), xlsx_warnings, headers_by_case


def _build_observed(
    gateway: LocalDataGateway,
    case_ids: list[str],
    headers_by_case: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, set[str]]:
    """Return ``{real_table: {real_col_set}}`` aggregated over the given cases.

    When ``headers_by_case`` is provided, columns come from CSV headers
    (schema-level — picks up empty tables too). Otherwise falls back to
    ``rows[0].keys()`` from the gateway (data-level — misses empty tables).
    """
    observed: dict[str, set[str]] = {}
    for cid in case_ids:
        if headers_by_case is not None and cid in headers_by_case:
            for table, cols in headers_by_case[cid].items():
                if cols:
                    observed.setdefault(table, set()).update(cols)
            continue
        gateway.set_case(cid)
        for table in gateway.list_tables():
            rows = gateway.query(table) or []
            if not rows:
                continue
            observed.setdefault(table, set()).update(rows[0].keys())
    return observed


def _samples_across_cases(
    gateway: LocalDataGateway, table: str, col: str, limit: int = 10,
) -> list:
    """Collect up to ``limit`` non-null sample values for ``col`` across cases."""
    out: list = []
    for cid in gateway.list_case_ids():
        gateway.set_case(cid)
        rows = gateway.query(table) or []
        for r in rows:
            v = r.get(col)
            if v is not None and v != "":
                out.append(v)
                if len(out) >= limit:
                    return out
    return out


def _siblings(gateway: LocalDataGateway, table: str, col: str) -> list[str]:
    """Return up to 25 sibling column names from any case's copy of the table."""
    for cid in gateway.list_case_ids():
        gateway.set_case(cid)
        rows = gateway.query(table) or []
        if rows:
            return [c for c in rows[0].keys() if c != col][:25]
    return []


# ── Reporting ────────────────────────────────────────────────────────────

def _print_summary(
    agg: adapter.AggregatedDiff,
    audit: adapter.ProfileOnlyAudit,
    observed: dict[str, set[str]],
    xlsx_warnings: list[str],
) -> None:
    print()
    print("DIFF SUMMARY")
    print("─" * 60)
    print(f"  Cases scanned         : {agg.case_count}")
    print(f"  Tables in real data   : {len(observed)}")
    print(f"  Auto-aliased columns  : {len(agg.auto_aliased)}  (already persisted)")
    print(f"  Ambiguous columns     : {len(agg.ambiguous)}  (need pick)")
    print(f"  New columns           : {len(agg.new_columns)}  (drafted, need verify)")
    new_tables_str = ", ".join(agg.new_tables) if agg.new_tables else "none"
    print(f"  New tables            : {len(agg.new_tables)}  ({new_tables_str})")
    print(f"  Profile-only tables   : {len(audit.profile_only_tables)}")
    print(f"  Profile-only columns  : {len(audit.profile_only_columns)}")
    if agg.dtype_conflicts:
        print(f"  ⚠ Dtype conflicts    : {len(agg.dtype_conflicts)}")
    if xlsx_warnings:
        print(f"  ⚠ Skipped (xlsx)     : {len(xlsx_warnings)} — {', '.join(xlsx_warnings[:3])}{'...' if len(xlsx_warnings) > 3 else ''}")
    print()


def _print_profile_only(audit: adapter.ProfileOnlyAudit) -> None:
    if not audit.profile_only_tables and not audit.profile_only_columns:
        return
    print("PROFILE-ONLY (in catalog but no real case has them — review manually)")
    print("─" * 60)
    for t in audit.profile_only_tables:
        print(f"  TABLE  {t}")
    by_table: dict[str, list[str]] = {}
    for entry in audit.profile_only_columns:
        by_table.setdefault(entry.table, []).append(entry.column)
    for t, cols in sorted(by_table.items()):
        print(f"  COLS   {t}: {', '.join(cols)}")
    print()


def _print_dtype_conflicts(agg: adapter.AggregatedDiff) -> None:
    if not agg.dtype_conflicts:
        return
    print("DTYPE CONFLICTS (same column, different dtype across cases)")
    print("─" * 60)
    for table, col, dtypes in agg.dtype_conflicts:
        print(f"  {table}.{col} : {', '.join(sorted(dtypes))}")
    print()


# ── Interactive review ──────────────────────────────────────────────────

def _group_by_table(entries: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in entries:
        out.setdefault(e.real_table, []).append(e)
    return out


def _write_alias(agent, cand, real_col: str) -> None:
    agent.catalog.write_profile_patch(cand.canonical_table, {
        "columns": {cand.canonical_col: {"aliases": [real_col]}}
    })


def _write_as_new(agent, real_table: str, real_col: str, real_dtype: str) -> None:
    agent.catalog.write_profile_patch(real_table, {
        "columns": {
            real_col: {
                "dtype": real_dtype,
                "description": "",
                "description_pending": True,
                "aliases": [real_col],
            }
        }
    })


def _print_one_ambiguous(idx: int, total: int, entry, samples: list) -> None:
    _say(
        f"\n  [{idx}/{total}] {entry.real_col}  (dtype: {entry.real_dtype})",
        style="bold",
    )
    if samples:
        _say(f"     samples: {', '.join(str(s) for s in samples)}", style="dim")
    for i, c in enumerate(entry.candidates, start=1):
        ok = "✓" if c.dtype_compatible else "✗"
        marker = "←" if i == 1 else " "
        line = (
            f"     {marker} {i}. {c.canonical_table}.{c.canonical_col}"
            f"  ratio={c.ratio:.2f}  dtype={c.canonical_dtype} {ok}"
        )
        _say(line, style="green" if i == 1 else "")


def _resolve_one_ambiguous(agent, entry, samples: list) -> None:
    """Single-entry resolution. ENTER = accept top candidate."""
    top = entry.candidates[0] if entry.candidates else None
    default_label = "1" if top else "s"
    choice = _ask(
        f"     pick [1-{len(entry.candidates)}], (n)ew, (s)kip, "
        "(t)ype canonical, (d) rewrite dtype",
        default=default_label,
    ).lower()

    if choice == "s":
        return
    if choice == "n":
        _write_as_new(agent, entry.real_table, entry.real_col, entry.real_dtype)
        _say(f"     → NEW under {entry.real_table}", style="yellow")
        return
    if choice == "t":
        typed = _ask("     canonical name").strip()
        if not typed:
            return
        target = entry.candidates[0].canonical_table if entry.candidates else entry.real_table
        agent.catalog.write_profile_patch(target, {
            "columns": {typed: {"aliases": [entry.real_col]}}
        })
        _say(f"     → aliased under {target}.{typed}", style="green")
        return
    if choice == "d":
        n_str = _ask(f"     rewrite dtype of which candidate [1-{len(entry.candidates)}]", "1")
        try:
            cand = entry.candidates[int(n_str) - 1]
        except (ValueError, IndexError):
            _say("     (invalid; skipping)", style="red")
            return
        agent.catalog.write_profile_patch(cand.canonical_table, {
            "columns": {
                cand.canonical_col: {
                    "dtype": entry.real_dtype,
                    "aliases": [entry.real_col],
                }
            }
        })
        _say(
            f"     → {cand.canonical_table}.{cand.canonical_col} dtype→{entry.real_dtype}, aliased",
            style="green",
        )
        return

    try:
        cand = entry.candidates[int(choice) - 1]
    except (ValueError, IndexError):
        _say("     (invalid choice — skipped)", style="red")
        return
    _write_alias(agent, cand, entry.real_col)
    _say(f"     → aliased under {cand.canonical_table}.{cand.canonical_col}", style="green")


def _resolve_ambiguous(
    agent,
    agg: adapter.AggregatedDiff,
    gateway: LocalDataGateway,
    *,
    auto_threshold: float = 0.0,
) -> None:
    if not agg.ambiguous:
        return

    # Pre-pass: auto-accept top candidate when ratio meets the threshold.
    pending = []
    auto_accepted = 0
    for entry in agg.ambiguous:
        if (
            auto_threshold > 0
            and entry.candidates
            and entry.candidates[0].ratio >= auto_threshold
            and entry.candidates[0].dtype_compatible
        ):
            _write_alias(agent, entry.candidates[0], entry.real_col)
            auto_accepted += 1
        else:
            pending.append(entry)

    _rule(f"AMBIGUOUS — {len(agg.ambiguous)} total")
    if auto_accepted:
        _say(
            f"  Auto-accepted {auto_accepted} (top ratio ≥ {auto_threshold})",
            style="cyan",
        )

    if not pending:
        return

    grouped = _group_by_table(pending)
    table_idx = 0
    for real_table, entries in grouped.items():
        table_idx += 1
        _rule(f"[{table_idx}/{len(grouped)}] {real_table} — {len(entries)} ambiguous", style="blue")
        # Per-table batch menu
        mode = _ask(
            "  (a)ccept all top / (r)eview each / (s)kip table / (q)uit interactive",
            default="a",
        ).lower()
        if mode == "q":
            _say("  (quitting interactive — re-run anytime to resume)", style="yellow")
            return
        if mode == "s":
            continue
        if mode == "a":
            written = 0
            for entry in entries:
                if entry.candidates:
                    _write_alias(agent, entry.candidates[0], entry.real_col)
                    written += 1
            _say(f"  → {written} aliased to top candidate.", style="green")
            continue
        # review each
        for i, entry in enumerate(entries, start=1):
            samples = _samples_across_cases(gateway, entry.real_table, entry.real_col, 5)
            _print_one_ambiguous(i, len(entries), entry, samples)
            _resolve_one_ambiguous(agent, entry, samples)


async def _verify_new_columns(
    agent,
    agg: adapter.AggregatedDiff,
    gateway: LocalDataGateway,
    *,
    accept_drafts: bool = False,
) -> None:
    if not agg.new_columns:
        return

    _rule(f"NEW COLUMNS — {len(agg.new_columns)} total")
    grouped = _group_by_table(agg.new_columns)
    table_idx = 0

    for real_table, entries in grouped.items():
        table_idx += 1
        _rule(f"[{table_idx}/{len(grouped)}] {real_table} — {len(entries)} new", style="blue")

        # Batch-draft in parallel for this table.
        siblings = _siblings(gateway, real_table, col="__none__")
        sample_map = {
            e.real_col: _samples_across_cases(gateway, real_table, e.real_col)
            for e in entries
        }
        _say(f"  Drafting {len(entries)} description(s) in parallel...", style="dim")
        drafts = await asyncio.gather(*[
            agent.draft_description(
                table=real_table,
                column=e.real_col,
                samples=sample_map[e.real_col],
                sibling_columns=[c for c in siblings if c != e.real_col],
                dtype=e.real_dtype,
            )
            for e in entries
        ])

        if accept_drafts:
            written = 0
            for e, d in zip(entries, drafts):
                final_text = d or e.drafted_description or ""
                if final_text:
                    agent.verify_description(real_table, e.real_col, new_text=final_text)
                    written += 1
            _say(f"  → {written} drafts auto-accepted.", style="green")
            continue

        mode = _ask(
            "  (a)ccept all drafts / (r)eview each / (s)kip table / (q)uit interactive",
            default="a",
        ).lower()
        if mode == "q":
            _say("  (quitting interactive)", style="yellow")
            return
        if mode == "s":
            continue
        if mode == "a":
            written = 0
            for e, d in zip(entries, drafts):
                final_text = d or e.drafted_description or ""
                if final_text:
                    agent.verify_description(real_table, e.real_col, new_text=final_text)
                    written += 1
            _say(f"  → {written} drafts accepted.", style="green")
            continue

        # review each
        for i, (e, d) in enumerate(zip(entries, drafts), start=1):
            samples = sample_map[e.real_col]
            _say(
                f"\n  [{i}/{len(entries)}] {e.real_col}  ({e.real_dtype})",
                style="bold",
            )
            if samples:
                _say(f"     samples: {', '.join(str(s) for s in samples[:5])}", style="dim")
            current = d or e.drafted_description or "(no draft)"
            while True:
                _say(f"     draft: {current}", style="cyan")
                choice = _ask(
                    "     (a)ccept / (e)dit / (r)egenerate / (s)kip",
                    default="a",
                ).lower()
                if choice == "s":
                    break
                if choice == "a":
                    if current and current != "(no draft)":
                        agent.verify_description(real_table, e.real_col, new_text=current)
                        _say("     → verified.", style="green")
                    break
                if choice == "e":
                    edited = _ask("     new description").strip()
                    if edited:
                        agent.verify_description(real_table, e.real_col, new_text=edited)
                        _say("     → verified with edit.", style="green")
                    break
                if choice == "r":
                    regen = await agent.draft_description(
                        table=real_table,
                        column=e.real_col,
                        samples=samples,
                        sibling_columns=[c for c in siblings if c != e.real_col],
                        dtype=e.real_dtype,
                    )
                    if regen:
                        current = regen
                    else:
                        _say("     (LLM blocked or unavailable)", style="red")


async def _verify_new_tables(
    agent,
    agg: adapter.AggregatedDiff,
    gateway: LocalDataGateway,
    *,
    accept_drafts: bool = False,
) -> None:
    if not agg.new_tables:
        return

    _rule(f"NEW TABLES — {len(agg.new_tables)} total")
    # Pre-fetch column lists + draft in parallel.
    cols_by_table = {t: _siblings(gateway, t, col="__none__") for t in agg.new_tables}
    _say("  Drafting table descriptions in parallel...", style="dim")
    drafts = await asyncio.gather(*[
        agent.draft_table_description(t, cols_by_table[t]) for t in agg.new_tables
    ])

    if accept_drafts:
        written = 0
        for t, d in zip(agg.new_tables, drafts):
            if d:
                agent.catalog.write_profile_patch(t, {"description": d})
                written += 1
        _say(f"  → {written} table descriptions auto-accepted.", style="green")
        return

    for i, (t, d) in enumerate(zip(agg.new_tables, drafts), start=1):
        cols = cols_by_table[t]
        _say(f"\n  [{i}/{len(agg.new_tables)}] {t}  ({len(cols)} cols)", style="bold")
        _say(f"     columns: {', '.join(cols[:15])}{'...' if len(cols) > 15 else ''}", style="dim")
        current = d or "(no draft)"
        _say(f"     draft: {current}", style="cyan")
        choice = _ask("     (a)ccept / (e)dit / (s)kip", default="a").lower()
        if choice == "s":
            continue
        if choice == "a":
            if current and current != "(no draft)":
                agent.catalog.write_profile_patch(t, {"description": current})
                _say("     → written.", style="green")
            continue
        if choice == "e":
            edited = _ask("     new description").strip()
            if edited:
                agent.catalog.write_profile_patch(t, {"description": edited})
                _say("     → written.", style="green")


# ── Main ────────────────────────────────────────────────────────────────

async def amain() -> None:
    parser = argparse.ArgumentParser(
        description="Sync canonical catalog with real-data CSVs."
    )
    parser.add_argument("--case-id", default=None,
                        help="Sync just one case (default: all real cases).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute diffs without writing to YAML.")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM description drafting.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Skip interactive review (just print the diff).")
    parser.add_argument("--include-simulated", action="store_true",
                        help="Also reconcile data_tables/simulated/ cases.")
    parser.add_argument("--draft-model", default="gpt-4.1",
                        help="Model used for LLM description drafting.")
    parser.add_argument("--auto-threshold", type=float, default=0.0,
                        help="Auto-accept ambiguous picks where the top "
                             "candidate's ratio ≥ THRESHOLD and dtype matches "
                             "(e.g. 0.95). Default 0 = no auto-accept.")
    parser.add_argument("--accept-drafts", action="store_true",
                        help="Accept LLM drafts for new columns + tables "
                             "without per-entry confirmation.")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    catalog = DataCatalog()

    sources: list[Path] = []
    if _REAL_DIR.is_dir():
        sources.append(_REAL_DIR)
    if args.include_simulated and _SIM_DIR.is_dir():
        sources.append(_SIM_DIR)
    if not sources:
        print(f"No data folder found at {_REAL_DIR}", file=sys.stderr)
        sys.exit(1)

    gateway, xlsx_warnings, headers_by_case = _load_gateway(sources)

    case_ids = [args.case_id] if args.case_id else gateway.list_case_ids()
    case_ids = [c for c in case_ids if c in gateway.list_case_ids()]
    if not case_ids:
        print("No cases to scan.", file=sys.stderr)
        sys.exit(1)
    print(f"Scanning {len(case_ids)} case(s): {', '.join(case_ids)}")
    if xlsx_warnings:
        print(
            f"  ⚠ Skipping {len(xlsx_warnings)} non-CSV file(s) — gateway is CSV-only: "
            f"{', '.join(xlsx_warnings[:3])}{'...' if len(xlsx_warnings) > 3 else ''}"
        )

    session_id = str(uuid.uuid4())[:8]
    logger = EventLogger(session_id=session_id)

    llm = None
    if not args.no_llm:
        try:
            from llm.firewall_stack import FirewallStack
            from llm.factory import build_llm
            firewall = FirewallStack(logger=logger)
            llm = build_llm(args.draft_model, firewall)
        except Exception as exc:
            print(f"  ⚠ LLM unavailable ({exc}); falling back to regex drafts.")
            llm = None

    from case_agents.data_manager_agent import DataManagerAgent
    agent = DataManagerAgent(gateway=gateway, catalog=catalog, llm=llm, logger=logger)

    canonical = {t: catalog._profiles[t]["columns"] for t in catalog.list_tables()}
    diffs: list[adapter.Diff] = []
    for cid in case_ids:
        diff = adapter.reconcile_case(gateway, canonical, cid)
        if not args.dry_run:
            adapter.apply_diff(diff, catalog)
        diffs.append(diff)

    agg = adapter.aggregate_diffs(diffs)
    observed = _build_observed(gateway, case_ids, headers_by_case=headers_by_case)
    audit = adapter.audit_profile_only(catalog, observed)

    _print_summary(agg, audit, observed, xlsx_warnings)
    _print_profile_only(audit)
    _print_dtype_conflicts(agg)

    if args.dry_run:
        print("(dry-run — no writes performed.)")
        return

    if args.non_interactive:
        print("(non-interactive — review skipped.)")
        return

    _resolve_ambiguous(agent, agg, gateway, auto_threshold=args.auto_threshold)
    if llm is not None:
        await _verify_new_columns(agent, agg, gateway, accept_drafts=args.accept_drafts)
        await _verify_new_tables(agent, agg, gateway, accept_drafts=args.accept_drafts)
    else:
        _say("\n(LLM disabled — skipping description-draft step.)", style="yellow")
        _say(
            "New columns/tables are written with description_pending=true; "
            "edit YAMLs by hand or re-run without --no-llm.",
            style="dim",
        )

    _say("\nDone.", style="bold green")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
