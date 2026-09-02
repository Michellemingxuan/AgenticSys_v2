"""Deterministic detection of specialist runs that rested on a failed tool call.

A specialist that fails HARD is already quarantined by `_record_failure`. A
specialist that SUCCEEDS on a broken tool result is not — it emits a well-formed
SpecialistOutput with fabricated numbers, which then flows into the KP, Amem,
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
    run from the KP, Amem and the chart channels (`_SkipPersistence`) — and its
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
# in, zero reported. The distiller then wrote that into the KP as a
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

# TWO PHRASINGS MATCH THE PATTERN WITHOUT ASSERTING AN ABSENCE, and both cost a
# wasted re-read in prod (`spend_payments` ran twice on several turns for this).
#
#   COMPLETENESS — "24/24 periods have data; no gaps in the record". What is
#   absent is the ABSENCE. Rows CONFIRM the claim, so the contradiction test has
#   its polarity inverted here: the specialist was right and paid for it.
#
#   SCOPED REMAINDER — "One failed payment is present. No evidence of ADDITIONAL
#   payment failures". The qualifier presupposes the instance the same sentence
#   just reported, so rows are expected, not contradictory.
#
# Deliberately NOT a whole-answer suppression: an answer can carry several
# absence phrasings, and one genuine denial among excused ones must still fire.
# So each match is judged in its own local window (see `_absence_is_excused`).
_COMPLETENESS_SUBJECT = re.compile(
    r"\b(gap|gaps|missing|omission|omissions|break|breaks|hole|holes|"
    r"blank|blanks|discontinuit|absence|absences|lapse|lapses)\b",
    re.IGNORECASE,
)
_SCOPED_REMAINDER = re.compile(
    r"\b(additional|further|other|more|another|beyond|second|repeat|repeated|"
    r"serial|subsequent|remaining|else)\b",
    re.IGNORECASE,
)
# How far around a match to look for the qualifier that excuses it. Wide enough
# for "No evidence of additional payment failures", tight enough that a genuine
# denial later in the same paragraph is not excused by an earlier one.
_EXCUSE_WINDOW = 40


# "no evidence of ..." is the ONE branch of `_ABSENCE_CLAIM` that does not
# require a countable noun — every other branch ends on record/row/transaction/
# payment/return/... so it is checkable by construction. What follows this one
# decides whether it is:
#
#   "No evidence of returned PAYMENTS"          countable -> keep checking
#   "No evidence of intentional structuring"    a JUDGEMENT -> nothing to count
#
# A judgement is an interpretation of a pattern. There is no `structuring`
# column, so rows coming back cannot contradict it — the check has no basis to
# challenge the claim, and firing costs a re-read that can never resolve.
#
# `case` is deliberately absent from this list even though `_ABSENCE_NOUNS`
# carries it: here it almost always means the credit case ("no evidence of X in
# this case"), not a countable row, and including it would make nearly every
# judgement look checkable.
_COUNTABLE_OBJECT = re.compile(
    r"\b(record|row|transaction|payment|return|entr|instance|match)",
    re.IGNORECASE,
)
_EVIDENCE_OF = re.compile(r"\bno\s+evidence\s+of\b", re.IGNORECASE)


def _is_judgement_claim(text: str, start: int, end: int) -> bool:
    """True for "no evidence of <abstraction>" — an interpretation, not a count.

    The match must be the BARE phrase. `_ABSENCE_CLAIM`'s countable branch is
    tried first and wins when a noun is within three words, so "No evidence of
    returned PAYMENTS" arrives here already consuming `payments` — leaving a
    tail of "in the data" that looks abstract. Requiring a full match on just
    "no evidence of" means anything the countable branch caught is countable by
    construction, and only the bare phrasing reaches the tail test.

    The object is then read to the end of the CLAUSE, not a fixed window: a
    later sentence mentioning transactions must not make this one look
    checkable.
    """
    if not _EVIDENCE_OF.fullmatch(text[start:end]):
        return False
    tail = re.split(r"[.;\n]", text[end:end + 80], maxsplit=1)[0]
    return not _COUNTABLE_OBJECT.search(tail)


def _absence_is_excused(text: str, start: int, end: int) -> bool:
    """True when THIS match cannot be contradicted by rows — a completeness or
    scoped-remainder phrasing, or a judgement with nothing countable behind."""
    window = text[max(0, start - _EXCUSE_WINDOW):end + _EXCUSE_WINDOW]
    return bool(_COMPLETENESS_SUBJECT.search(window)
                or _SCOPED_REMAINDER.search(window)
                or _is_judgement_claim(text, start, end))


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


# `summarize_by_group` appends a remainder row when it truncates to a top-N
# ("7 more groups"). It is an aggregate of what was dropped, not a category, so
# it must not count towards "more than one value is present".
_TAIL_GROUP = re.compile(r"^\s*(?:…|\.\.\.|\d[\d,]*\s+more\b)", re.IGNORECASE)


# The nouns `_ABSENCE_CLAIM`'s countable branches end on. The denied noun is
# whichever of these the matched phrase closes with — "no payment returns"
# denies `return`, not `payment`.
_ABSENCE_NOUNS = ("record", "row", "transaction", "payment", "return",
                  "entr", "instance", "case", "match")


def _denied_noun(absence_phrase: str) -> str | None:
    """The noun an absence phrase negates, or None for the noun-less phrasings
    ("none were found", "no evidence of ..."). Read from the END of the match
    because the earlier words are modifiers: in "no payment returns" it is the
    returns that are denied, and `payment` merely qualifies them."""
    for w in reversed(_WORD.findall(absence_phrase.lower())):
        for noun in _ABSENCE_NOUNS:
            if w.startswith(noun):
                return noun
    return None


def _grouped_dimension_verdict(output: str, claim_text: str,
                               absence_phrase: str = ""):
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
    # IS THIS GROUP-BY ABOUT THE THING BEING DENIED? Matching any shared word
    # was far too loose: "No PAYMENT returns are present" and a breakdown by
    # `Payment Bank Account` share "payment", so a split across six bank
    # accounts was read as proof that returns exist. That is the prod false
    # positive — `counts_seen: []`, contradicted purely by a group-by that was
    # never about returns.
    #
    # The gate is the DENIED NOUN — the noun the absence phrase actually
    # negates, which `_ABSENCE_CLAIM` always ends on. "no payment returns" and
    # "zero returns" both deny `return`, so `Return Flag` qualifies and
    # `Payment Bank Account` does not. When the phrasing carries no noun
    # ("no evidence of …"), fall back to the old shared-word test rather than
    # going silent.
    denied = _denied_noun(absence_phrase) if absence_phrase else None
    if denied is not None:
        if not any(t.startswith(denied) or denied.startswith(t) for t in tokens):
            return None
    else:
        claim_words = set(_WORD.findall(claim_text.lower()))
        # Singular/plural tolerance: "returns" in the claim matches "Return Flag".
        if not any(t in claim_words or t + "s" in claim_words for t in tokens):
            return None
    groups = payload.get("groups")
    if not isinstance(groups, list) or not groups:
        return None

    # Only groups that actually hold rows count as evidence of presence, and
    # the synthetic tail entry (`{"…": "N more groups truncated"}` / a
    # "N more" label) is a remainder, not a category.
    labels = [str(g.get("group")) for g in groups
              if isinstance(g, dict) and g.get("group") is not None
              and "…" not in g
              and not _TAIL_GROUP.match(str(g.get("group")))
              and not (isinstance(g.get("n_records"), int) and g["n_records"] <= 0)]

    # Nothing, or one category = the dimension is uniform = every other
    # category IS absent. That supports the claim.
    if len(labels) <= 1:
        return False

    # More than one CATEGORY WITH ROWS = the denied one is among them.
    return True


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
        # Every match, not the first: an answer can pair an excused phrasing
        # with a genuine denial, and the genuine one must still be checked.
        m = next((hit for hit in _ABSENCE_CLAIM.finditer(claim_text)
                  if not _absence_is_excused(claim_text, hit.start(), hit.end())),
                 None)
        if m is None:
            return None

        matched: list[int] = []
        grouped: list[bool] = []
        for _tool, _cid, output in _iter_call_outcomes(result):
            if not isinstance(output, str):
                continue
            matched += [int(x) for x in _ROWS_MATCHING.findall(output)]
            matched += [int(x.replace(",", "")) for x in _COUNT_RESULT.findall(output)]
            verdict = _grouped_dimension_verdict(
                output, claim_text, absence_phrase=m.group(0))
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
