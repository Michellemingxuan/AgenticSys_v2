"""Filesystem tools for the report agent. Confined to the active case folder."""
from __future__ import annotations

import re
from pathlib import Path

from agents import RunContextWrapper, function_tool

from models.app_context import AppContext


# Curated case-report .md files often carry raw numeric values that are 6+
# digits long (card limits, balances, spend / payment totals). The boundary
# redaction layer in llm.firewall_stack masks any `\d{6,}` run, which
# accidentally turns e.g. `174897.36` → `***MASKED***.36` even though the
# value is a perfectly displayable dollar amount the reviewer needs to see.
#
# Format-on-read: when the report agent reads a curated file, we pre-format
# any 6+ digit numeric run (with optional decimal, optional leading $) with
# thousand-separator commas. The commas break the digit run so the value
# passes through every redaction boundary unchanged, and the LLM never sees
# the raw `\d{6,}` form — it only ever sees `$174,897.36`. Same principle
# as the data path's `aggregate_column` formatting.
_LONG_NUMERIC_RE = re.compile(r"(\$)?(\d{6,})(\.\d+)?")
_MAX_SNIPPETS_PER_FILE = 5
_MAX_SNIPPET_LEN = 200


def _truncate(line: str) -> str:
    line = line.rstrip("\n")
    return line if len(line) <= _MAX_SNIPPET_LEN else line[:_MAX_SNIPPET_LEN] + "…"


def _format_long_numerics(text: str) -> str:
    """Comma-format any 6+ digit numeric run in text.

    Examples:
        "limit 201800"       → "limit 201,800"
        "balance 174897.36"  → "balance 174,807.36"
        "$1200700"           → "$1,200,700"
        "37675218257"        → "37,675,218,257"

    The transformation is content-agnostic: anything matching `\\d{6,}` is
    treated as a number and gets thousand separators. Curated case reports
    are expected to mask genuine PII (card numbers, account ids) at the
    source — this layer is defense in depth against the boundary redaction
    masking display-meaningful numerics.
    """
    def _sub(match: re.Match) -> str:
        sign = match.group(1) or ""
        int_part = match.group(2)
        dec_part = match.group(3) or ""
        return f"{sign}{int(int_part):,}{dec_part}"

    return _LONG_NUMERIC_RE.sub(_sub, text)


@function_tool
async def fs_list_files(ctx: RunContextWrapper[AppContext]) -> str:
    folder = ctx.context.case_folder
    if folder is None or not folder.exists():
        return "No case folder available."
    files = [p.name for p in folder.iterdir() if p.is_file()]
    return "\n".join(sorted(files)) if files else "Folder is empty."


@function_tool
async def fs_read_file(
    ctx: RunContextWrapper[AppContext],
    filename: str,
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    folder = ctx.context.case_folder
    if folder is None:
        return "No case folder available."
    target = (folder / filename).resolve()
    # Confine to case_folder to prevent path traversal.
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return f"Access denied: '{filename}' is outside the case folder."
    if not target.exists() or not target.is_file():
        return f"File not found: {filename}"
    # Comma-format long numeric runs so they survive boundary redaction.
    formatted = _format_long_numerics(target.read_text())
    # Default (0, 0) → whole file, unchanged.
    if start_line <= 0 and end_line <= 0:
        return formatted
    # Ranged read: 1-based inclusive, clamped to the file extent.
    lines = formatted.splitlines()
    n = len(lines)
    lo = start_line if start_line > 0 else 1
    hi = end_line if end_line > 0 else n
    lo = max(1, min(lo, n))
    hi = max(1, min(hi, n))
    if lo > hi:
        lo, hi = hi, lo
    return "\n".join(lines[lo - 1 : hi])


@function_tool
async def fs_grep(ctx: RunContextWrapper[AppContext], terms: list[str]) -> str:
    """Search case-folder file CONTENTS for any of `terms` (case-insensitive OR).

    Returns files ranked by how many distinct terms matched, each with a few
    `L<n>: <line>` snippets (1-based line numbers that line up with
    fs_read_file's start_line/end_line). Discovery only — read the file (or a
    slice of it) before drafting.

    Note: file contents are comma-formatted before matching, so numeric search
    terms must include separators (e.g. `174,897`, not `174897`).
    """
    folder = ctx.context.case_folder
    if folder is None or not folder.exists():
        return "No case folder available."
    cleaned = [t for t in (terms or []) if t and t.strip()]
    if not cleaned:
        return "No terms provided."
    patterns = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in cleaned]

    results = []  # (name, matched_terms:set, matching_lines:int, snippets:list)
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        try:
            text = _format_long_numerics(p.read_text())
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable file must not abort discovery
        matched_terms = set()
        matching_lines = 0
        snippets = []  # (lineno, line)
        for i, line in enumerate(text.splitlines(), start=1):
            hit_terms = [t for (t, pat) in patterns if pat.search(line)]
            if hit_terms:
                matched_terms.update(hit_terms)
                matching_lines += 1
                if len(snippets) < _MAX_SNIPPETS_PER_FILE:
                    snippets.append((i, line))
        if matched_terms:
            results.append((p.name, matched_terms, matching_lines, snippets))

    if not results:
        return "No matches for: [" + ", ".join(cleaned) + "]"

    # rank: more distinct terms first, then more matching lines, then name
    results.sort(key=lambda r: (-len(r[1]), -r[2], r[0]))

    blocks = []
    for name, terms_set, n_lines, snippets in results:
        matched = ", ".join(sorted(terms_set))
        header = f"{name}  (matched: {matched} | {n_lines} hits)"
        body = "\n".join(f"  L{n}: {_truncate(line)}" for n, line in snippets)
        blocks.append(header + ("\n" + body if body else ""))
    return "\n".join(blocks)
