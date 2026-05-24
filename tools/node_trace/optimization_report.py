"""Memory / tokens / latency optimization rollups over the node_trace DB.

Usage:
    python -m tools.node_trace.optimization_report memory
    python -m tools.node_trace.optimization_report tokens
    python -m tools.node_trace.optimization_report latency
    python -m tools.node_trace.optimization_report --all
    python -m tools.node_trace.optimization_report --turn <turn_id>
"""
from __future__ import annotations

import argparse
from pathlib import Path

from tools.node_trace._io import open_db


_DEFAULT_DB = Path("logs/node_traces.db")


def memory_section(db: Path, turn_id: str | None = None) -> str:
    conn = open_db(db)
    where_turn = "AND turn_id = ?" if turn_id else ""
    params = (turn_id,) if turn_id else ()
    rows = conn.execute(
        f"""
        SELECT
          substr(node, 1, instr(node || '.round_', '.round_') - 1) AS specialist,
          node,
          prompt_tokens
        FROM node_trace
        WHERE node LIKE '%.round_%' AND prompt_tokens IS NOT NULL {where_turn}
        ORDER BY specialist, started_at
        """,
        params,
    ).fetchall()
    if not rows:
        return "[memory] no per-round data yet.\n"
    out = ["[memory] per-specialist context growth per round:"]
    by_spec: dict[str, list[int]] = {}
    for r in rows:
        by_spec.setdefault(r["specialist"], []).append(r["prompt_tokens"])
    for spec, series in by_spec.items():
        growth_pct = (
            int((series[-1] - series[0]) * 100 / series[0])
            if series and series[0] else 0
        )
        out.append(
            f"  {spec:<40} rounds={len(series):<3}  "
            f"start={series[0]:<6}  end={series[-1]:<6}  "
            f"growth={growth_pct:+d}%   series={series}"
        )
    return "\n".join(out) + "\n"


def tokens_section(db: Path, turn_id: str | None = None) -> str:
    conn = open_db(db)
    where_turn = "WHERE turn_id = ?" if turn_id else ""
    params = (turn_id,) if turn_id else ()
    summary = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(prompt_tokens), 0)       AS total_prompt,
          COALESCE(SUM(completion_tokens), 0)   AS total_completion,
          COALESCE(SUM(cached_input_tokens), 0) AS total_cached,
          COALESCE(SUM(reasoning_tokens), 0)    AS total_reasoning,
          COALESCE(SUM(cost_usd), 0.0)          AS total_cost
        FROM node_trace {where_turn}
        """,
        params,
    ).fetchone()
    cache_ratio = (
        summary["total_cached"] / summary["total_prompt"]
        if summary["total_prompt"] else 0.0
    )
    top = conn.execute(
        f"""
        SELECT node,
               SUM(prompt_tokens + completion_tokens) AS toks,
               SUM(cost_usd) AS dollars,
               AVG(CAST(cached_input_tokens AS REAL) /
                   NULLIF(prompt_tokens, 0)) AS cache_ratio
        FROM node_trace {where_turn}
        GROUP BY node
        ORDER BY toks DESC
        LIMIT 10
        """,
        params,
    ).fetchall()
    lines = [
        "[tokens] aggregate:",
        f"  total_prompt:     {summary['total_prompt']:,}",
        f"  total_completion: {summary['total_completion']:,}",
        f"  total_cached:     {summary['total_cached']:,}  "
        f"(cache ratio: {cache_ratio:.1%})",
        f"  total_reasoning:  {summary['total_reasoning']:,}",
        f"  total_cost:       ${summary['total_cost']:.4f}",
        "",
        "[tokens] top-10 spenders by node:",
    ]
    for r in top:
        cr = r["cache_ratio"] or 0.0
        lines.append(
            f"  {r['node']:<50} toks={r['toks'] or 0:<8}  "
            f"${(r['dollars'] or 0.0):.4f}  cache={cr:.1%}"
        )
    return "\n".join(lines) + "\n"


def latency_section(db: Path, turn_id: str | None = None) -> str:
    conn = open_db(db)
    where_turn = "WHERE turn_id = ?" if turn_id else ""
    params = (turn_id,) if turn_id else ()
    rows = conn.execute(
        f"""
        SELECT node,
               AVG(duration_ms)  AS avg_total,
               AVG(queue_wait_ms) AS avg_queue,
               AVG(llm_call_ms)  AS avg_llm,
               AVG(overhead_ms)  AS avg_overhead,
               AVG(ttft_ms)      AS avg_ttft,
               COUNT(*)          AS n
        FROM node_trace {where_turn}
        GROUP BY node
        ORDER BY avg_total DESC
        LIMIT 15
        """,
        params,
    ).fetchall()
    lines = [
        "[latency] top-15 slowest nodes (avg per call):",
        f"  {'node':<45} {'total':>7}  {'queue':>6}  {'llm':>6}  "
        f"{'over':>6}  {'ttft':>6}  n",
    ]
    for r in rows:
        def f(v):
            return "  -  " if v is None else f"{int(v)}ms"
        lines.append(
            f"  {r['node']:<45} {f(r['avg_total']):>7}  "
            f"{f(r['avg_queue']):>6}  {f(r['avg_llm']):>6}  "
            f"{f(r['avg_overhead']):>6}  {f(r['avg_ttft']):>6}  {r['n']}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("section", nargs="?", choices=["memory", "tokens", "latency"])
    p.add_argument("--db", default=str(_DEFAULT_DB))
    p.add_argument("--turn", help="scope to one turn_id")
    p.add_argument("--all", action="store_true", help="print all three sections")
    args = p.parse_args()

    db = Path(args.db)
    if args.all or args.section is None:
        print(memory_section(db, args.turn))
        print(tokens_section(db, args.turn))
        print(latency_section(db, args.turn))
        return
    if args.section == "memory":
        print(memory_section(db, args.turn))
    elif args.section == "tokens":
        print(tokens_section(db, args.turn))
    elif args.section == "latency":
        print(latency_section(db, args.turn))


if __name__ == "__main__":
    main()
