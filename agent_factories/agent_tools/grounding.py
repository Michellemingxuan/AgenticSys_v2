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
    """Return a reason string when `output` signals a failed tool call, else None.

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

    Returns `[{"tool", "call_id", "reason", "excerpt"}, ...]` in transcript
    order. Empty list means every tool call this run either succeeded or was
    successfully re-issued.
    """
    errors: list[dict] = []
    recovered_tools: set[str] = set()
    tools_with_errors: set[str] = set()

    for tool, call_id, output in _iter_call_outcomes(result):
        reason = classify_tool_output(tool, output)
        if reason is None:
            # A clean call retroactively clears earlier failures of the SAME
            # tool — the specialist fixed its call and re-issued it.
            recovered_tools.add(tool)
        else:
            # Only record the first error for each tool; retries that also fail
            # don't add additional errors.
            if tool not in tools_with_errors:
                errors.append({
                    "tool": tool,
                    "call_id": call_id,
                    "reason": reason,
                    "excerpt": output[:_EXCERPT_CHARS],
                })
                tools_with_errors.add(tool)

    return [e for e in errors if e["tool"] not in recovered_tools]
