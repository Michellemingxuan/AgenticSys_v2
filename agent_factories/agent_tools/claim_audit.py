"""Deterministic audit of a specialist's CLAIMS against its own tool outputs.

`grounding.scan_tool_errors` asks "did a tool fail?". This asks the next
question: "does the answer match what the tools actually returned?" Same input
(the completed run's item list), same properties — pure, no I/O, no LLM, no
shared state — so it costs nothing on the critical path and is fully testable.

SHADOW MODE. Nothing here gates behavior: `agent_tool` logs the findings and
moves on. Prose→number extraction is inherently fuzzy (`2.7x`, `$3.93M`, a
legitimately derived ratio, a rounded figure), and this system has already been
burned twice by over-flagging — the bureau partial-batch false positive and a
DATA GAP branch that told specialists to abandon a real column. So: measure the
false-positive rate on a known-good question suite FIRST, then decide what may
gate a retry. Never wire an auditor straight to the quarantine.

FALSE POSITIVES, AS MEASURED. Three consecutive live runs produced nothing but
noise — `['2025', '$404,152']`, `['$392K', '$319K', '17%', '17%']`,
`['67%', '33%', '67%', '33%']` — from three distinct causes, all now handled:

  - bare YEARS read as quantities ("fell sharply by 2025"). Masked.
  - correctly ROUNDED values ("$392K" for a tool's $392,454.63) missed by an
    exact match. Each written number now carries the tolerance its own
    precision implies.
  - COMPUTED percentages, which no tool ever emits verbatim. Own bucket.

Three checks:

1. `unsupported_numbers` — magnitudes in `findings` / `evidence` that appear
   nowhere in the run's tool outputs. Catches fabrication and bad arithmetic.

2. `derived_not_checkable` — figures written as a percentage or multiple, i.e.
   arithmetic ON tool values. Not evidence of anything; listed so a human can
   look, never a gate. Deliberately NOT "verified by re-derivation": measured,
   60 of 60 random percentages could be reconstructed as a ratio of two numbers
   drawn from the 101 that a mere two tool calls emit. Every percentage
   "verifies", including a fabricated one, so re-deriving would launder bad
   numbers rather than catch them.

3. `sample_size_as_count` — a claimed count that equals a truncated display
   sample (`rows_returned`) while the true count (`rows_matching_filter`)
   differs. Very low FP: those two keys come from the same payload, so the
   coincidence is nearly always the known bug of counting `rows[]`.

DERIVED NUMBERS ARE ALLOWED IN ANSWERS. This is a decision, not an oversight —
do not "fix" `derived_not_checkable` into a gate. Small derivations are fine
for a model to do: "2 of 3 cards are consumer -> 67%" will not come out wrong,
and forcing a tool call for every such figure buys latency, not accuracy.

The failure this system actually suffered was never the arithmetic. It was the
OPERANDS: "top merchant is 10.0% of spend" was computed over all history while
the question asked about 2025. Right division, wrong denominator. So the
control lives elsewhere, and deliberately so:

  - `claim_audit.scope_line` / `measured_over` put the table and time window
    behind every answer into the trace, which is what lets a reviewer catch a
    correct number measured over the wrong set.
  - `aggregate_column` op=`share` (with `base_filter_*`) and op=`ratio` exist
    for the cases where the DENOMINATOR is a genuine choice — a share of a
    filtered base, top-1-among-top-5. Those are worth a tool call because the
    choice is the thing that can be wrong; dividing 2 by 3 is not.

Gating on derived figures would fire on nearly every answer while catching none
of the errors that have actually occurred here.
"""
from __future__ import annotations

import json
import re

from agent_factories.agent_tools.grounding import _iter_call_outcomes
from agent_factories.agent_tools.series_extract import _values_match


# Dates are not quantities. `2025-04` would otherwise contribute 2025 and 4, and
# a period cited in prose would look like an unsupported number. Masked out of
# the claim text before any number is read.
_DATE_LIKE = re.compile(
    r"\b\d{4}-\d{1,2}(?:-\d{1,2})?\b"          # 2025-04, 2025-04-01
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"            # 04/01/2025
    r"|\b[A-Z][a-z]{2,8}'?-?\d{2,4}\b"         # Jul-25, July'2023
    # A BARE YEAR. Measured on real runs, this was the single largest source of
    # false positives — "spend fell sharply by 2025" contributed 2025 as an
    # unsupported quantity in nearly every audited answer. Restricted to
    # 1900-2099 so ordinary four-digit quantities are untouched; a genuine count
    # that happens to equal a year is the accepted cost, and this is advisory
    # output, never a gate.
    r"|\b(?:19|20)\d{2}\b"
)

# A number with optional currency, thousands separators, and a scale/unit
# suffix. Deliberately permissive on the way IN — normalization below decides
# what it means.
_NUMBER = re.compile(
    r"(?<![\w.])"
    r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?"
    r"\s*(%|[KMB]\b|bn\b|x\b)?",
    re.IGNORECASE,
)

_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}


# Relative slack for a number written at full precision. Anything coarser gets
# a tolerance derived from how it was ROUNDED — see `_candidate_values`.
_DEFAULT_REL_TOL = 0.001


def _candidate_values(raw_int: str, decimals: str,
                      suffix: str) -> list[tuple[float, float]]:
    """`[(value, absolute_tolerance), ...]` — every reading a written number
    could plausibly mean, each with the slack its own precision implies.

    A claim of "41%" may be backed by a tool value of 41 or 0.41; "$3.93M" by
    3930000. Returning all readings makes the check LENIENT — the point is to
    surface numbers with no backing at all, not to police formatting.

    ROUNDING is why the tolerance travels with the value. Specialists write
    "$392K" for a tool's `$392,454.63`; matched exactly that reads as
    fabrication, and it was the second-largest false positive on real runs.
    The tolerance is ONE UNIT of the last digit written, which covers both
    conventions seen in real answers: rounding ($392,454 -> "$392K") and
    truncating ($174,897 -> "$174K", where rounding would have given "$175K").
    A unit is still tight — "$392K" will not match $450,000.
    """
    try:
        base = float(f"{raw_int.replace(',', '')}{decimals or ''}")
    except ValueError:
        return []
    s = (suffix or "").strip().lower()
    n_decimals = len(decimals) - 1 if decimals else 0      # `decimals` keeps '.'

    def _rel(v: float) -> tuple[float, float]:
        return (v, abs(v) * _DEFAULT_REL_TOL)

    if s in _SCALE:
        scaled = base * _SCALE[s]
        unit_step = (10.0 ** -n_decimals) * _SCALE[s]
        return [(scaled, max(unit_step, abs(scaled) * _DEFAULT_REL_TOL)),
                _rel(base)]
    if s == "%":
        unit_step = 10.0 ** -n_decimals
        return [(base, max(unit_step, abs(base) * _DEFAULT_REL_TOL)),
                (base / 100.0, max(unit_step / 100.0,
                                   abs(base / 100.0) * _DEFAULT_REL_TOL))]
    unit_step = 10.0 ** -n_decimals
    return [(base, max(unit_step, abs(base) * _DEFAULT_REL_TOL))]


def _numbers_in(text: str) -> list[tuple[str, list[tuple[float, float]]]]:
    """`[(as_written, [(value, tolerance), ...]), ...]`, dates excluded."""
    if not isinstance(text, str) or not text:
        return []
    masked = _DATE_LIKE.sub(" ", text)
    out: list[tuple[str, list[tuple[float, float]]]] = []
    for m in _NUMBER.finditer(masked):
        values = _candidate_values(m.group(1), m.group(2), m.group(3))
        if values:
            out.append((m.group(0).strip(), values))
    return out


# Percentages, ratios and multiples are ARITHMETIC on tool values, so they are
# never quoted verbatim by a tool. Recognised by how they are written rather
# than by trying to re-derive them — see `audit_claims` for why re-derivation
# is worthless here.
_COMPUTED_SUFFIX = ("%", "x")


def _looks_computed(written: str) -> bool:
    return written.strip().lower().endswith(_COMPUTED_SUFFIX)


def _claim_text(final_output) -> str:
    """`findings` + `evidence` — what the specialist actually asserts."""
    parts: list[str] = []
    findings = getattr(final_output, "findings", None)
    if isinstance(findings, str):
        parts.append(findings)
    evidence = getattr(final_output, "evidence", None)
    if isinstance(evidence, list):
        parts.extend(str(e) for e in evidence)
    return "\n".join(parts)


def _output_values(result) -> set[float]:
    """Every number the tools returned this run.

    Read from the raw output text rather than parsed JSON so a value inside a
    nested string (batch results carry their payload as a string) still counts.
    Tolerances are dropped here — a tool output is exact; the slack belongs to
    the CLAIM, which is what gets rounded.
    """
    values: set[float] = set()
    for _tool, _cid, output in _iter_call_outcomes(result):
        for _raw, candidates in _numbers_in(output):
            values.update(v for v, _tol in candidates)
    return values


def _count_pairs(result) -> list[tuple[int, int]]:
    """`(rows_returned, rows_matching_filter)` pairs where the two DIFFER.

    Only from payloads that carry both keys, so the comparison is always within
    one tool result.
    """
    pairs: list[tuple[int, int]] = []
    for _tool, _cid, output in _iter_call_outcomes(result):
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        returned = payload.get("rows_returned")
        matching = payload.get("rows_matching_filter")
        if isinstance(returned, int) and isinstance(matching, int) \
                and returned != matching:
            pairs.append((returned, matching))
    return pairs


def audit_claims(result, final_output) -> dict:
    """`{"unsupported_numbers": [...], "sample_size_as_count": [...]}`.

    Empty lists mean nothing suspicious. Never raises — a broken audit must not
    break the turn it is auditing.
    """
    report: dict = {"unsupported_numbers": [], "derived_not_checkable": [],
                    "sample_size_as_count": []}
    try:
        claims = _numbers_in(_claim_text(final_output))
        if not claims:
            return report
        supported = _output_values(result)

        for written, candidates in claims:
            if any(abs(value - v) <= tol
                   for value, tol in candidates for v in supported):
                continue
            # A COMPUTED figure will not appear verbatim in any tool output,
            # and this auditor cannot check it either way. Reporting it as
            # "unsupported" reads as fabrication; staying silent hides it. So
            # it gets its own bucket, and the name says what is true: not
            # checkable here.
            #
            # It is tempting to go further and try to RECONSTRUCT it — confirm
            # 67% as a/b for some pair of tool values. Measured, that proves
            # nothing: 60 of 60 random percentages were reconstructible from
            # the 101 numbers a mere two tool calls emit. Any percentage
            # "verifies", including a fabricated one, so the check would launder
            # bad numbers rather than catch them.
            (report["derived_not_checkable"] if _looks_computed(written)
             else report["unsupported_numbers"]).append(written)

        # A claimed count equal to a truncated sample size, when the true count
        # differs, is the known "counted rows[] instead of rows_matching_filter"
        # error rather than a coincidence.
        for returned, matching in _count_pairs(result):
            for written, candidates in claims:
                if any(abs(value - float(returned)) <= tol
                       for value, tol in candidates):
                    report["sample_size_as_count"].append({
                        "claimed": written,
                        "rows_returned": returned,
                        "rows_matching_filter": matching,
                    })
    except Exception:  # noqa: BLE001 - an auditor must never break the turn
        return report
    return report


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
