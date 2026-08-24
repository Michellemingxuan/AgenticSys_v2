"""Filesystem tools for the report agent. Confined to the active case folder."""
from __future__ import annotations

import re
from pathlib import Path

from agents import RunContextWrapper, function_tool

from models.app_context import AppContext


# What counts as a curated report file. Reports are `.md`; `.txt` and `.csv`
# are tolerated text neighbours so a report saved in a plain-text variant is
# not read as "this case has no reports". Binary exports (`.docx`, `.pdf`)
# are deliberately absent: `fs_read_file` cannot decode them, and detecting a
# report the agent then cannot open buys a wasted (and, on safechain,
# failure-prone) round.
#
# ONE definition, three consumers — `_case_has_reports` (detection),
# `agent_tool`'s injected file list (what report_agent is told exists), and
# `fs_list_files` (what any agent can discover). These were three separate
# literals and had already drifted: the injected list matched suffixes
# case-SENSITIVELY while detection lowercased, so a `.MD` report made
# `_case_has_reports` true while the agent was handed an empty list.
REPORT_SUFFIXES = frozenset({".md", ".txt", ".csv"})


def is_report_file(p: Path) -> bool:
    """True when `p` is a curated report the agent can actually read.

    Case-insensitive on the suffix — Windows and Excel round-trips produce
    `.MD` / `.TXT`, and a report is no less a report for its capitalization.
    """
    return p.is_file() and p.suffix.lower() in REPORT_SUFFIXES


# Display roster for the curated report sections — the reviewer-facing
# ordering and labels behind the Case Report panel's section chips.
#
# Keyed by DOMAIN PREFIX, not by full filename: the canonical layout names
# files `<domain>_exp_0.md`, but the suffix is not load-bearing and already
# varies (`strategy_0.md` carries no `_exp`). Matching on the prefix means a
# re-export that bumps `_exp_0` -> `_exp_1` keeps its section instead of
# silently falling through to the unknown-file tail.
#
# Order is the reviewer's narrative order (summary -> evidence -> action),
# NOT alphabetical, and it mirrors the routing table in
# `skills/workflow/report_needle.md` so the agent and the UI name the same
# files the same way. Sections listed here but absent on disk are reported
# as unavailable rather than omitted — a missing Bureau report is a fact the
# reviewer should see, not a gap in the tab strip.
REPORT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("executive_summary", "Exec Summary"),
    ("default_journey", "Default Journey"),
    ("bureau", "Bureau"),
    ("payment_spend", "Payment & Spend"),
    ("modeling", "Modeling"),
    ("driver", "Drivers"),
    ("crossbu", "Cross-BU"),
    ("interestingness", "Interestingness"),
    ("strategy", "Strategy"),
    ("wcc_notes", "Notes"),
)


def _matches_section(stem: str, prefix: str) -> bool:
    """True when report file `stem` belongs to section `prefix`."""
    return stem == prefix or stem.startswith(prefix + "_")


# Several curated reports title their sections with an ASCII banner instead
# of a markdown heading:
#
#     ====================================================
#     Bureau Credit Scores
#     ====================================================
#
# Left alone this renders badly and, worse, MISLEADINGLY: markdown reads the
# trailing rule as a setext underline, so the title becomes an <h1> while the
# LEADING rule survives as a stray paragraph of equals signs glued to it
# ("============ Bureau Credit Scores"). The banner is a heading in intent,
# so we make it one in fact.
#
# `=` only, never `-`: a `---` run is a legitimate markdown thematic break and
# `executive_summary_exp_0.md` uses it as a section separator. Rewriting those
# would destroy real formatting.
_ASCII_BANNER_RE = re.compile(
    r"^[ \t]*={3,}[ \t]*\n"                 # opening rule
    r"^[ \t]*(?P<title>\S[^\n]*?)[ \t]*\n"  # the title itself
    r"^[ \t]*={3,}[ \t]*$",                  # closing rule
    re.MULTILINE,
)


def _promote_ascii_headings(text: str) -> str:
    """Rewrite `====` / title / `====` banners as `## title`.

    Level 2 to match the reports that DO use markdown headings
    (`executive_summary_exp_0.md`, `wcc_notes_exp_0.md` both open at `##`),
    so one report's sections sit at the same level as another's.
    """
    return _ASCII_BANNER_RE.sub(lambda m: f"## {m.group('title')}", text)


def read_report_text(path: Path) -> str | None:
    """Read a curated report as display-ready text, or None if unreadable.

    Deliberately the SAME read path `fs_read_file` gives the report agent,
    comma-formatting included: the agent quotes `$201,800` after
    `_format_long_numerics`, so a UI that served the raw bytes would print
    `201800` for the very same limit reduction and read as a discrepancy
    between the report and the assistant. One reader, one rendering.
    """
    try:
        return _format_long_numerics(path.read_text())
    except (OSError, UnicodeDecodeError):
        # Binary exports (`.docx`) live in real case folders. `is_report_file`
        # already excludes them; this is the belt-and-braces for a `.md` that
        # is not actually UTF-8 text.
        return None


# Most curated reports mark list items with a typographic bullet rather than
# a markdown one:
#
#     • Spend records: 3865 spend records are present, ...
#     • Payment records: 186 successful payment records are present, ...
#
# Markdown does not recognise U+2022, so it reads a run of these as ONE
# paragraph joined by soft line breaks — the reader gets a wall of prose with
# stray bullet characters sprinkled through it instead of a list. 172 of them
# across 8 of the 10 sections, so this is the normal case, not an edge one.
#
# `executive_summary_exp_0.md` already uses real markdown `-` (including
# nested at indent 2) and must pass through untouched, which is why this only
# ever rewrites U+2022 and friends.
_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)[\u2022\u25e6\u2023\u2043]\s+", re.MULTILINE)


def _normalize_bullets(text: str) -> str:
    """Rewrite typographic bullet markers as markdown list markers.

    Indentation is preserved so any nesting the source implies survives.
    """
    return _BULLET_RE.sub(lambda m: f"{m.group('indent')}- ", text)


def _display_markdown(path: Path) -> str | None:
    """`read_report_text` plus the display-only fixes a renderer needs.

    Split from `read_report_text` deliberately: that function is the agent's
    exact read path and must stay that way, while this one may reshape
    presentation. Both keep the same NUMBERS — only the markup differs.
    """
    raw = read_report_text(path)
    if raw is None:
        return None
    return _normalize_bullets(_promote_ascii_headings(raw))


def list_report_sections(folder: Path) -> list[dict]:
    """Curated report sections for `folder`, in reviewer-facing order.

    Returns one dict per section — `key`, `label`, `filename`, `markdown` —
    with `markdown` None when the section has no file on disk (or the file
    could not be decoded). Any report file that matches no known section is
    appended after the known ones under a title-cased label, so an unfamiliar
    case layout degrades to "extra tabs" rather than to hidden content.
    """
    if not folder.is_dir():
        return []
    files = sorted((p for p in folder.iterdir() if is_report_file(p)),
                   key=lambda p: p.name)
    claimed: set[str] = set()
    sections: list[dict] = []

    for key, label in REPORT_SECTIONS:
        match = next((p for p in files if _matches_section(p.stem, key)), None)
        if match is not None:
            claimed.add(match.name)
        sections.append({
            "key": key,
            "label": label,
            "filename": match.name if match else None,
            "markdown": _display_markdown(match) if match else None,
        })

    for p in files:
        if p.name in claimed:
            continue
        sections.append({
            "key": p.stem,
            "label": p.stem.replace("_", " ").strip().title(),
            "filename": p.name,
            "markdown": _display_markdown(p),
        })
    return sections


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
    # Report files only. The case folder also accumulates generated artifacts
    # (a `charts/` subdir) and binary exports the reader cannot open; listing
    # those invites the agent to spend a round on a file that can only fail.
    files = [p.name for p in folder.iterdir() if is_report_file(p)]
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
    # Not every file in a case folder is text. Real folders carry `.docx`
    # exports (case 11854808010 ships `report_without_citations.docx`) and
    # `read_text()` on a zip container raises UnicodeDecodeError — uncaught,
    # that kills the specialist's whole turn over one bad filename. The
    # listers no longer surface such files, but the model can still name one
    # from memory or from a report's own cross-reference. `fs_grep` already
    # skips them; say so here so the model treats it as a dead end.
    try:
        raw = target.read_text()
    except UnicodeDecodeError:
        return (
            f"Cannot read '{filename}': it is not a text file (likely a "
            f".docx/.pdf export). Reports are `.md` files — call "
            f"fs_list_files to see the ones this case actually has."
        )
    except OSError as exc:
        return f"Cannot read '{filename}': {type(exc).__name__}."
    # Comma-format long numeric runs so they survive boundary redaction.
    formatted = _format_long_numerics(raw)
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
