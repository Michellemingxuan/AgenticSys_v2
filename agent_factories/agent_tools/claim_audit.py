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

Two checks, both chosen for low false-positive risk:

1. `unsupported_numbers` — numeric literals in `findings` / `evidence` that
   appear nowhere in the run's tool outputs. Catches fabrication and bad
   arithmetic. Expected noise: values the specialist legitimately DERIVED
   (sums, ratios, percentages) won't appear verbatim, which is exactly what the
   shadow period is for measuring.

2. `sample_size_as_count` — a claimed count that equals a truncated display
   sample (`rows_returned`) while the true count (`rows_matching_filter`)
   differs. Very low FP: those two keys come from the same payload, so the
   coincidence is nearly always the known bug of counting `rows[]`.
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


def _candidate_values(raw_int: str, decimals: str, suffix: str) -> list[float]:
    """Every reading a written number could plausibly mean.

    A claim of "41%" may be backed by a tool value of 41 or 0.41; "$3.93M" by
    3930000. Returning all readings makes the check LENIENT — the point is to
    surface numbers with no backing at all, not to police formatting.
    """
    try:
        base = float(f"{raw_int.replace(',', '')}{decimals or ''}")
    except ValueError:
        return []
    s = (suffix or "").strip().lower()
    if s in _SCALE:
        return [base * _SCALE[s], base]
    if s == "%":
        return [base, base / 100.0]
    return [base]


def _numbers_in(text: str) -> list[tuple[str, list[float]]]:
    """`[(as_written, [possible values]), ...]` from prose, dates excluded."""
    if not isinstance(text, str) or not text:
        return []
    masked = _DATE_LIKE.sub(" ", text)
    out: list[tuple[str, list[float]]] = []
    for m in _NUMBER.finditer(masked):
        values = _candidate_values(m.group(1), m.group(2), m.group(3))
        if values:
            out.append((m.group(0).strip(), values))
    return out


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
    """
    values: set[float] = set()
    for _tool, _cid, output in _iter_call_outcomes(result):
        for _raw, candidates in _numbers_in(output):
            values.update(candidates)
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
    report: dict = {"unsupported_numbers": [], "sample_size_as_count": []}
    try:
        claims = _numbers_in(_claim_text(final_output))
        if not claims:
            return report
        supported = _output_values(result)

        for written, candidates in claims:
            if not any(_values_match(c, v) for c in candidates for v in supported):
                report["unsupported_numbers"].append(written)

        # A claimed count equal to a truncated sample size, when the true count
        # differs, is the known "counted rows[] instead of rows_matching_filter"
        # error rather than a coincidence.
        for returned, matching in _count_pairs(result):
            for written, candidates in claims:
                if any(_values_match(c, float(returned)) for c in candidates):
                    report["sample_size_as_count"].append({
                        "claimed": written,
                        "rows_returned": returned,
                        "rows_matching_filter": matching,
                    })
    except Exception:  # noqa: BLE001 - an auditor must never break the turn
        return report
    return report
