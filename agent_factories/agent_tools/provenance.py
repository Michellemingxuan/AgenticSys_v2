"""What a specialist's answer was MEASURED OVER — table, column, filters, window.

Provenance, not verification. The reviewer's own check is the strongest one
available: shown "top merchant = 10.0%", they cannot tell whether the base was
the whole history or one month, but shown "base = all 8,888 rows" they can —
and they know the domain, which no automated check does.

Forcing this into the OUTPUT SCHEMA (a required `scope` per evidence bullet)
worked but cost tokens on every bullet, against the per-specialist latency
budget. This gets the same information for FREE: the scope is already in the
arguments the specialist passed, so it is derived deterministically instead of
asking the model to restate what it just typed.

Two surfaces, deliberately different in grain:

  `measured_over`  per-call lines for the REASONING TRACE
  `scope_line`     the whole run collapsed to `table: window` pairs, terse
                   enough for a reviewer to read in passing

History: this module was created during an evaluation push and also carried
`audit_claims`, a shadow-mode check on whether the answer's NUMBERS traced to
tool outputs. That work belongs to AgenticEval, whose `content/` pipeline does
it properly — aggregate-aware, oracle-backed, with a considered position on
false positives. It was removed here rather than maintained in two places; the
runtime keeps only what a reviewer reads. See `grounding.py` for the checks
that remain, which exist because they must act DURING a turn.
"""

from __future__ import annotations

import json
import re

from agent_factories.agent_tools.grounding import _iter_call_outcomes
from agent_factories.agent_tools.series_extract import _values_match


# ── provenance: what the answer was measured over ───────────────────────────
#
# The reviewer's own check is the strongest one available: shown "top merchant
# = 10.0%", they cannot tell whether the base was the whole history or one
# month, but shown "base = all 8,888 rows" they can — and they know the domain,
# which no automated check does.
#
# Forcing that into the OUTPUT SCHEMA (a required `scope` per evidence bullet)
# worked but cost tokens on every bullet, against the per-specialist latency
# budget. This gets the same information for FREE: the scope is already in the
# arguments the specialist passed, so derive it deterministically instead of
# asking the model to restate what it just typed.

_SCOPE_TOOLS = frozenset({
    "query_table", "batch_query_table", "aggregate_column", "batch_aggregate",
    "summarize_trend", "batch_summarize_trend", "summarize_by_group",
    "join_table", "transaction_detail", "score_driver_values",
})

_MAX_SCOPE_LINES = 8

# Filters render structurally, so this is generous enough that a realistic
# multi-filter call fits whole. Raised from the old 120 because the previous
# budget was spent on JSON punctuation rather than on the filters themselves.
_MAX_FILTER_CHARS = 160


def _iter_calls(result):
    """Yield `(tool_name, params)` for each function_call in the transcript."""
    try:
        items = result.to_input_list()
    except (AttributeError, TypeError):
        return
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        if not name:
            continue
        args = item.get("arguments")
        if isinstance(args, str):
            try:
                params = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                params = {}
        else:
            params = args or {}
        yield name, (params if isinstance(params, dict) else {})


def _clip(text: str, limit: int) -> str:
    """Truncate VISIBLY. A silent cut reads as a complete line, so nobody
    checks the part that went missing."""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _filter_bits(value) -> str:
    """Render a filters list as `column op value; …` instead of raw JSON.

    The raw form was dumped and hard-cut at 120 chars, which sliced mid-token
    (`"op":"gte","valu`) and — the real damage — dropped the THRESHOLD, which
    is exactly what a reviewer needs to judge whether a number was measured
    over the right set. Structured, the same filters run about half as long,
    so they usually survive intact; matches the `where X eq 'v'` idiom already
    used for the single-filter case.
    """
    items = value
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (json.JSONDecodeError, ValueError):
            return _clip(items, _MAX_FILTER_CHARS)
    if not isinstance(items, list):
        return _clip(str(value), _MAX_FILTER_CHARS)
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        column = item.get("column") or item.get("name") or "?"
        op = item.get("op") or "eq"
        raw = item.get("value")
        text = ",".join(str(v) for v in raw) if isinstance(raw, list) else str(raw)
        # `between` carries its endpoints as "lo,hi"; `..` reads as a range and
        # keeps it distinguishable from an `in` list.
        parts.append(f"{column} {op} {text.replace(',', '..') if op == 'between' else text}")
    return _clip("; ".join(parts), _MAX_FILTER_CHARS)


def _one_scope(tool: str, p: dict) -> str:
    """One compact line naming the table, column, op and every filter."""
    table = p.get("table_name") or p.get("left_table") or p.get("base_table") or ""
    col = (p.get("column") or p.get("value_column") or "")
    head = f"{table}.{col}" if table and col else (table or col or "?")

    bits: list[str] = []
    if p.get("op"):
        bits.append(f"op={p['op']}")
    if p.get("group_column"):
        bits.append(f"by {p['group_column']}")
    if p.get("time_column"):
        bits.append(f"on {p['time_column']}")
    if p.get("period"):
        bits.append(f"per {p['period']}")
    if p.get("denominator_column"):
        bits.append(f"/ {p['denominator_column']}")
    if p.get("filter_column") and p.get("filter_value") is not None:
        bits.append(f"where {p['filter_column']} "
                    f"{p.get('filter_op', 'eq')} {p['filter_value']!r}")
    if p.get("filters"):
        bits.append(f"filters=[{_filter_bits(p['filters'])}]")
    if p.get("base_filter_column"):
        bits.append(f"base={p['base_filter_column']} "
                    f"{p.get('base_filter_op', 'eq')} {p.get('base_filter_value')!r}")
    if p.get("timestamps"):
        bits.append(f"timestamps={_clip(str(p['timestamps']), 60)}")
    for k in ("top_n", "limit"):
        if p.get(k):
            bits.append(f"{k}={p[k]}")
    if p.get("specs_json"):
        bits.append(f"specs={_clip(str(p['specs_json']), 140)}")

    return f"{tool}({head}" + (f", {', '.join(bits)}" if bits else "") + ")"


def measured_over(result) -> list[str]:
    """Compact provenance lines for the data calls behind this answer.

    Deterministic, zero LLM cost, and un-forgettable — unlike a directive asking
    the specialist to state its scope. Duplicates collapse so a repeated call
    doesn't pad the list.
    """
    out: list[str] = []
    try:
        for tool, params in _iter_calls(result):
            if tool not in _SCOPE_TOOLS:
                continue
            line = _one_scope(tool, params)
            if line not in out:
                out.append(line)
            if len(out) >= _MAX_SCOPE_LINES:
                break
    except Exception:  # noqa: BLE001 - provenance must never break the turn
        return out
    return out


# ── the one-line version, for the reviewer-facing answer ────────────────────
#
# `measured_over` is per-call and belongs in the trace. The FINAL ANSWER needs
# something a reviewer reads in passing, so this collapses a whole run to
# `table: window` pairs — the two things a wrong-scope answer gets wrong. Kept
# deliberately terse: a footnote nobody reads catches nothing.

# Any date-ish literal in a filter value, so a window can be spotted without
# knowing which column happens to be the date one in each table.
_WINDOW = re.compile(r"\d{4}-\d{2}(?:-\d{2})?(?:\s*(?:\.\.|,|to)\s*\d{4}-\d{2}(?:-\d{2})?)?")


def _window_of(params: dict) -> str:
    """The date window a call constrained to, or "" when unconstrained."""
    blob = " ".join(
        str(params.get(k) or "")
        for k in ("filter_value", "filters", "timestamps",
                  "base_filter_value", "start_date", "end_date")
    )
    found = [f.replace(",", "..").replace(" to ", "..") for f in _WINDOW.findall(blob)]
    if not found:
        return ""
    lo, hi = min(found), max(found)
    return lo if lo == hi else f"{lo}..{hi}"


def _scope_targets(params: dict):
    """The ``(table, params)`` pairs one call constrained.

    The batch tools (`batch_aggregate`, `batch_summarize_trend`) carry their
    tables inside `specs_json` instead of a top-level `table_name`, so looking
    only at the direct params drops those calls entirely — and a specialist
    that used ONLY batch tools then produced an empty scope, scoring 0 on
    provenance even though `measured_over` had captured every table. Yield
    each spec's table so the batch tools count like any other call.
    """
    table = (params.get("table_name") or params.get("left_table")
             or params.get("base_table") or "")
    if table:
        yield str(table), params
        return
    specs = params.get("specs_json")
    if isinstance(specs, str):
        try:
            specs = json.loads(specs)
        except (json.JSONDecodeError, ValueError):
            return
    for spec in specs if isinstance(specs, list) else []:
        if not isinstance(spec, dict):
            continue
        spec_table = (spec.get("table_name") or spec.get("left_table")
                      or spec.get("base_table") or "")
        if spec_table:
            # The spec's own filters win; anything it omits falls back to the
            # enclosing call, which is where a shared window usually lives.
            yield str(spec_table), {**params, **spec}


def scope_line(result) -> str:
    """`spends: all dates; model_scores_transaction: 2025-05-01..2025-05-31`.

    "all dates" is the load-bearing half — an unconstrained table answering a
    windowed question is the error this exists to expose, and silence would
    read as fine.
    """
    windows: dict[str, set] = {}
    try:
        for tool, params in _iter_calls(result):
            if tool not in _SCOPE_TOOLS:
                continue
            for table, scoped in _scope_targets(params):
                windows.setdefault(table, set()).add(_window_of(scoped))
    except Exception:  # noqa: BLE001
        return ""

    parts: list[str] = []
    for table, seen in windows.items():
        real = sorted(w for w in seen if w)
        parts.append(f"{table}: {', '.join(real) if real else 'all dates'}")
    return "; ".join(parts)
