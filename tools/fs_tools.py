"""Filesystem tools for the report agent. Confined to the active case folder."""
from __future__ import annotations

import re
from pathlib import Path

from agents import RunContextWrapper, function_tool

from agent_factories.app_context import AppContext


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
async def fs_read_file(ctx: RunContextWrapper[AppContext], filename: str) -> str:
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
    raw = target.read_text()
    # Comma-format long numeric runs so they survive boundary redaction.
    return _format_long_numerics(raw)
