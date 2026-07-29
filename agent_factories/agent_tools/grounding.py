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

# The ONE tool for which "table not found" is a benign negative rather than a
# failure: schema probing is normal exploration — the specialist learns the
# table is absent from this case and picks another. Every other tool asking for
# that table wanted DATA and got none. See data_tools.py:1014 vs :1237-:2995.
_SCHEMA_PROBE_TOOLS = frozenset({"get_table_schema"})

# Matches both "table 'x' not found for current case" and the
# transaction_detail variant "base table 'x' not found for current case".
_TABLE_NOT_FOUND = re.compile(r"table '[^']*' not found for current case")


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

    # Order matters: the specs_unparseable payload also carries an "error" key,
    # so it must be classified before the generic batch-element check.
    if "did NOT run" in text:
        return "specs_unparseable"

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
    """
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
