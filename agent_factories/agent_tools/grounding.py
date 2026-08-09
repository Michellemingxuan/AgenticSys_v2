"""Deterministic detection of specialist runs that rested on a failed tool call.

A specialist that fails HARD is already quarantined by `_record_failure`. A
specialist that SUCCEEDS on a broken tool result is not — it emits a well-formed
SpecialistOutput with fabricated numbers, which then flows into the KB, Amem,
and the next turn's episodic context. This module is the detector for that case.

Pure: no I/O, no LLM, no project imports. Reads a completed run's own item list,
so it is scoped correctly by construction — no shared state, no assumptions
about concurrency between specialists.

Detection is by marker strings in the tool output rather than a structured
side-channel. `data_tools._log_result` does receive the exact classification in
its `extra` dict, but the data tools are sync `def`s that the SDK may run in a
thread executor while specialists run concurrently — scoping a per-run ledger
across that boundary risks attributing one specialist's error to another. The
brittleness that string-matching introduces is closed by the drift-guard test in
`tests/test_tools/test_data_tools_error_markers.py`, which parses the real
literals out of `data_tools.py` and asserts this module still classifies them.
"""
from __future__ import annotations

import json
import re

_EXCERPT_CHARS = 300

# The tools for which "table not found" is a benign negative rather than a
# failure: DISCOVERY is normal exploration — the specialist learns the table is
# absent from this case and picks another. Every other tool asking for that
# table wanted DATA and got none. See data_tools.py:1014 vs :1237-:2995.
#
# `search_columns` belongs here for the same reason `get_table_schema` does:
# both answer "what is there?", and probing a name that turns out not to be a
# table is how that question gets asked. Flagging it would quarantine a
# specialist for exploring — the same over-flagging that made an honest DATA
# GAP report indistinguishable from fabrication.
_SCHEMA_PROBE_TOOLS = frozenset({"get_table_schema", "search_columns"})

# Matches both "table 'x' not found for current case" and the
# transaction_detail variant "base table 'x' not found for current case".
_TABLE_NOT_FOUND = re.compile(r"table '[^']*' not found for current case")

# Emitted by `data_tools` when a column exists but is EMPTY for this case. The
# tool succeeded and reported an absence, so this is not a failed call — it is
# the answer. Bound to the emitter by the drift guard in
# `tests/test_tools/test_data_tools_error_markers.py`.
_DATA_GAP_MARKER = "DATA GAP:"


def classify_tool_output(tool: str, output: str) -> str | None:
    """Reason string when `output` signals a failed tool call, else None.

    Thin wrapper over :func:`classify_tool_output_detailed` for callers that
    only need the reason (the drift guard in
    `tests/test_tools/test_data_tools_error_markers.py`, mostly).
    """
    detail = classify_tool_output_detailed(tool, output)
    return detail["reason"] if detail else None


def classify_tool_output_detailed(tool: str, output: str) -> dict | None:
    """`{"reason", "partial", "n_failed", "n_total"}` when `output` signals a
    failed tool call, else None.

    `partial` is the load-bearing field. A BATCH tool returns one result per
    spec, and a single bad spec among several used to condemn the whole call —
    which quarantined answers built on the specs that DID succeed. Observed in
    prod: `bureau_data` carries all-blank columns (SBFE Score and friends) for
    some cases, so a trend over one legitimately reports "no parseable values"
    — an honest DATA GAP, not a broken tool — and that one element flagged
    bureau's perfectly good FICO and delinquency numbers as unsupported.

    So: a partial batch failure is still worth a retry (the specialist can fix
    the bad spec, and in the observed run it did), but it must NOT quarantine
    the run. Only a call that failed OUTRIGHT does that.
    """
    batch = _classify_batch(tool, output)
    if batch is not None:
        return batch
    reason = _classify_scalar(tool, output)
    if reason is None:
        return None
    return {"reason": reason, "partial": False, "n_failed": 1, "n_total": 1}


def _classify_batch(tool: str, output: str) -> dict | None:
    """Per-element classification for batch tools, or None if not a batch.

    Batch payloads are `{"results": [{"index", "result", ...}, ...]}`. A batch
    that never RAN (malformed specs_json) is emitted as a bare string instead,
    so it doesn't parse here and falls through to the scalar path as a total
    failure — which is correct: nothing ran.
    """
    if not isinstance(output, str) or "results" not in output:
        return None
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    results = parsed.get("results")
    if not isinstance(results, list) or not results:
        return None

    reasons: list[str | None] = []
    for element in results:
        inner = element.get("result") if isinstance(element, dict) else element
        if not isinstance(inner, str):
            inner = "" if inner is None else json.dumps(inner, default=str)
        reasons.append(_classify_scalar(tool, inner))

    failed = [r for r in reasons if r is not None]
    if not failed:
        return None
    return {
        "reason": failed[0],
        "partial": len(failed) < len(reasons),
        "n_failed": len(failed),
        "n_total": len(reasons),
    }


def _classify_scalar(tool: str, output: str) -> str | None:
    """Reason for a SINGLE tool output.

    `tool` participates in the decision — "table not found" is benign from
    `get_table_schema` and a real gap from every data-retrieving tool.
    """
    if not isinstance(output, str) or not output:
        return None
    text = output.strip()

    # BENIGN NEGATIVE, checked first. "This case has no data for that column" is
    # the tool WORKING — same category as `get_table_schema` reporting a table
    # is absent. Flagging it made a specialist that honestly reported the gap
    # indistinguishable from one that fabricated numbers, and quarantined the
    # honest one (see `_DATA_GAP_MARKER`'s emitter in data_tools).
    if _DATA_GAP_MARKER in text:
        return None

    # Order matters: the specs_unparseable payload also carries an "error" key,
    # so it must be classified before the generic batch-element check.
    if "did NOT run" in text:
        return "specs_unparseable"

    # A column the caller named that isn't there — a correctable mistake, so
    # it must be FLAGGED (retryable), unlike the benign DATA GAP above.
    if "COLUMN NOT FOUND" in text:
        return "column_not_found"

    if "no parseable" in text:
        return "no_groups" if "group" in tool else "no_buckets"

    if _TABLE_NOT_FOUND.search(text):
        return None if tool in _SCHEMA_PROBE_TOOLS else "table_not_found"

    if "data layer is not initialized" in text:
        return "data_layer_uninitialized"

    # A BARE "Data unavailable" means the catalog is None — a dead data layer.
    # This reaches us from get_table_schema too (data_tools.py:1006, :1089), and
    # the schema carve-out above must NOT swallow it: the carve-out is keyed on
    # the "table '<x>' not found" text, which this does not contain.
    if text == "Data unavailable":
        return "data_layer_uninitialized"

    if '"error"' in text:
        return "spec_rejected"

    return None


def _iter_call_outcomes(result):
    """Yield (tool, call_id, output) in transcript order.

    Pairs `function_call` items with their `function_call_output` by `call_id`;
    parallel tool calls interleave, so positional pairing would mis-bind. Falls
    back to the most recent unmatched call when `call_id` is absent.
    """
    try:
        items = result.to_input_list()
    except (AttributeError, TypeError):
        return

    names_by_id: dict[str, str] = {}
    unmatched: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call":
            name = item.get("name")
            if not name:
                continue
            call_id = item.get("call_id")
            if call_id:
                names_by_id[call_id] = name
            unmatched.append(name)
        elif itype == "function_call_output":
            call_id = item.get("call_id")
            tool = names_by_id.get(call_id) if call_id else None
            if tool is None:
                tool = unmatched[-1] if unmatched else "?"
            yield tool, (call_id or ""), (item.get("output") or "")


def scan_tool_errors(result) -> list[dict]:
    """Errors that were NOT superseded by a later clean call to the same tool.

    Returns at most one entry per tool, carrying that tool's latest unrecovered
    failure. Supersession is order-sensitive: a clean call clears only failures
    that came BEFORE it. A tool whose final call returned an error (even if an
    earlier call succeeded) is reported, because the run's current state is that
    the tool is broken.

    `[{"tool", "call_id", "reason", "excerpt"}, ...]` in transcript order.

    FAILS OPEN. This is the one check here with teeth — a hit quarantines the
    run from the KB, Amem and the chart channels (`_SkipPersistence`) — and its
    call site in `agent_tool` is not itself guarded, so an exception raised here
    would propagate into the AgentsException handler and record the specialist
    as a HARD FAILURE. A crash in the detector would then destroy the very
    answer it was checking, over a malformed transcript item rather than
    anything wrong with the work.

    So a broken scan returns NO ERRORS: "the detector could not run" must mean
    "no evidence of a problem", never "assume the worst". The same rule the
    other checkers in this family already follow.
    """
    try:
        errors_by_tool: dict[str, dict] = {}

        for tool, call_id, output in _iter_call_outcomes(result):
            detail = classify_tool_output_detailed(tool, output)
            if detail is None:
                # A clean call clears only what came BEFORE it.
                errors_by_tool.pop(tool, None)
            else:
                errors_by_tool[tool] = {
                    "tool": tool,
                    "call_id": call_id,
                    "reason": detail["reason"],
                    # `partial` — some specs in a batch succeeded. Callers should
                    # retry on these but NOT quarantine the run; see
                    # classify_tool_output_detailed.
                    "partial": detail["partial"],
                    "n_failed": detail["n_failed"],
                    "n_total": detail["n_total"],
                    "excerpt": output[:_EXCERPT_CHARS],
                }

        return list(errors_by_tool.values())
    except Exception:  # noqa: BLE001 — a checker must never break the turn
        return []


# ── absence asserted against rows that came back ────────────────────────────
#
# The other checks here ask "did a tool FAIL". This asks the one question that
# caught nothing before: does the answer DENY what a tool returned?
#
# Measured, case 11854808010. The specialist issued exactly the right call —
# `query_table(payments, "Return Flag" eq "1")` — the tool returned
# `rows_matching_filter: 1` with the row populated ($105,818.60 on 2025-04-28,
# INSUFFICIENT FUNDS), and the answer said "No payment returns were found;
# there are zero records in the payments table with Return Flag == 1". One row
# in, zero reported. The distiller then wrote that into the KB as a
# high-confidence knowledge point, so every later turn inherited it as fact.
#
# Nothing existing could see it: the tool did not fail, no filter matched zero,
# the claim carried no number to trace. It is a pure misreading of a correct
# result, and the only evidence is that the two disagree.
#
# THE RULE, and it is narrow on purpose: an assertion of absence must be backed
# by a tool result that actually returned NOTHING. If every data call in the run
# came back with rows, "none/zero/no such" has no source. Requiring a zero
# SOMEWHERE is what keeps the false-positive rate near nil — a specialist that
# legitimately found nothing always has that zero to point at.

_ABSENCE_CLAIM = re.compile(
    r"\b(?:"
    r"no\s+(?:such\s+)?(?:\w+\s+){0,3}(?:record|row|transaction|payment|return|entr|instance|case|match)"
    r"|zero\s+(?:\w+\s+){0,3}(?:record|row|transaction|payment|return|entr|instance|case|match)"
    r"|none\s+(?:were|was|found|present)"
    r"|(?:were|was)\s+(?:not\s+)?(?:found|identified|present|observed)"
    r"|(?:did\s+not|does\s+not|didn't|doesn't)\s+(?:have|show|contain|find)"
    r"|no\s+evidence\s+of"
    r")",
    re.IGNORECASE,
)

# `rows_matching_filter` is the true count; `rows_returned` is a display sample.
_ROWS_MATCHING = re.compile(r'"rows_matching_filter"\s*:\s*(\d+)')
# Countable results that legitimately establish an absence.
_COUNT_RESULT = re.compile(r"=\s*(?:count\s*)?(\d[\d,]*)\b")


# `summarize_by_group` never emits a zero-count group — it lists only the values
# PRESENT — so it can never supply the zero the rule above asks for, and the
# check used to fail open on it. The enumeration is still decisive, just read
# differently: the group SET is the answer.
#
#   case 366132845011   groups [{"group": "0", n: 357}]              1 group
#   case 11854808010    groups [{"group":"0",n:31}, {"group":"1",n:1}]  2 groups
#
# Both answers said "zero returns". The first is TRUE — the dimension is
# uniform, so every other category really is absent. The second is FALSE — the
# enumeration shows a second category with rows in it.
#
# Only applied when the claim is ABOUT the grouped dimension. A group-by on
# `Merchant Name` says nothing about returned payments, and firing on it would
# be exactly the over-flagging this module is built to avoid.
_WORD = re.compile(r"[a-z]+")


def _grouped_dimension_verdict(output: str, claim_text: str):
    """`True` (claim is contradicted) / `False` (enumeration supports it) /
    `None` (this group-by is not about the claim)."""
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "groups" not in payload:
        return None
    col = str(payload.get("group_column") or "")
    tokens = {t for t in _WORD.findall(col.lower()) if len(t) > 3}
    if not tokens:
        return None
    claim_words = set(_WORD.findall(claim_text.lower()))
    # Singular/plural tolerance: "returns" in the claim matches "Return Flag".
    if not any(t in claim_words or t + "s" in claim_words for t in tokens):
        return None
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        return None
    n = payload.get("n_groups_total")
    n = n if isinstance(n, int) else len(groups)
    # One group = the dimension is uniform = every other category IS absent.
    return n > 1


def absence_contradicted_by_rows(result, final_output) -> dict | None:
    """`{claim, max_rows_matching, calls}` when the answer asserts absence and
    NO data call in the run returned zero. `None` otherwise.

    Never raises — a broken check must not break the turn it is checking.
    """
    try:
        parts: list[str] = []
        for attr in ("findings", "evidence"):
            v = getattr(final_output, attr, None)
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, list):
                parts.extend(str(x) for x in v)
        claim_text = "\n".join(parts)
        m = _ABSENCE_CLAIM.search(claim_text)
        if not m:
            return None

        matched: list[int] = []
        grouped: list[bool] = []
        for _tool, _cid, output in _iter_call_outcomes(result):
            if not isinstance(output, str):
                continue
            matched += [int(x) for x in _ROWS_MATCHING.findall(output)]
            matched += [int(x.replace(",", "")) for x in _COUNT_RESULT.findall(output)]
            verdict = _grouped_dimension_verdict(output, claim_text)
            if verdict is not None:
                grouped.append(verdict)
        # An enumeration of the claimed dimension settles it outright, in both
        # directions — it is stronger evidence than a row count elsewhere.
        if grouped:
            if not any(grouped):
                return None
            return {
                "claim": claim_text[max(0, m.start() - 60):m.end() + 60].strip(),
                "max_rows_matching": max(matched) if matched else -1,
                "counts_seen": sorted(set(matched))[:8],
                "contradicted_by": "grouped dimension has >1 value present",
            }
        if not matched:
            return None
        # A zero anywhere is the specialist's licence to assert absence.
        if any(n == 0 for n in matched):
            return None
        return {
            "claim": claim_text[max(0, m.start() - 60):m.end() + 60].strip(),
            "max_rows_matching": max(matched),
            "counts_seen": sorted(set(matched))[:8],
        }
    except Exception:  # noqa: BLE001 — a checker must never break the turn
        return None
