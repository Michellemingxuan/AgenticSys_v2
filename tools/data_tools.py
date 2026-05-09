"""Data-access tool functions for agent tool-calling.

All queries are scoped to the currently active case. The case_id is set on the
gateway at session start — tools don't need to specify it.
"""

from __future__ import annotations

import json
import operator
import re
from typing import Any, Callable

from agents import function_tool
from datalayer.catalog import DataCatalog
from datalayer.gateway import DataGateway

# Module state — guarded against autoreload reset.
# In notebooks with `%autoreload 2`, re-executing this module's top level
# would reset these to None and silently break the session (the gateway the
# notebook just initialized would vanish). The try/except preserves whatever
# `init_tools()` last set across reloads.
try:
    _gateway  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _gateway: DataGateway | None = None
try:
    _catalog  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _catalog: DataCatalog | None = None
try:
    _logger  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _logger: Any = None  # logger.event_logger.EventLogger when wired; None = silent

# Per-(case_id, table_name) schema cache. The output of ``get_table_schema``
# is deterministic per case — gateway data + catalog profile + sync-applied
# patches don't change after first-open — so memoizing the full result string
# avoids redundant catalog walks when multiple specialists probe the same
# table within a turn (or across turns within the same case session).
# Module-global so it spans turns; key includes ``case_id`` so cross-case
# contamination is impossible. Cleared explicitly via ``init_tools`` /
# ``clear_schema_cache`` so test fixtures with mutating state stay correct.
try:
    _schema_cache  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _schema_cache: dict[tuple[str | None, str], str] = {}

_MAX_CHARS = 3000
_LOG_PREVIEW_CHARS = 500  # how much of tool output to snapshot in tool_result events

_FILTER_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


# Lightweight table-name normalization for real→canonical resolution.
# Mirrors datalayer.adapter._normalize_name without importing the adapter
# module (which pulls pandas — sync-time only).
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_TRAILING_DIGITS = re.compile(r"\d+$")


def _normalize(name: str) -> str:
    return _TRAILING_DIGITS.sub("", _NON_ALNUM.sub("", name.lower()))


def _resolve_canonical_table(real_table: str) -> str | None:
    """Find the primary canonical table name that matches a real table name.

    Returns the highest-priority match from the cascade in
    :func:`_resolve_canonical_tables` (or ``None`` if nothing matches).
    """
    matches = _resolve_canonical_tables(real_table)
    return matches[0] if matches else None


def _resolve_canonical_tables(real_table: str) -> list[str]:
    """Find all canonical tables relevant to a real table, in priority order.

    Matching cascade:
      1. Exact key in catalog ``_profiles``.
      2. Table-level ``aliases`` declared in any canonical profile (e.g.
         ``model_scores.yaml`` declares ``aliases: [modelling_data]``).
      3. Equal under normalization (case/punctuation only).
      4. Substring overlap of normalized forms (``bureau`` ⊂ ``bureau_data``).

    Returns a deduped list — the first entry is the primary match, the rest
    are fallbacks. Useful when a hand-written real-data profile (like
    ``bureau_data.yaml``) only carries a subset of columns and the rest
    need to be looked up under the broader canonical (``bureau.yaml``).
    """
    if _catalog is None:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            out.append(name)

    if real_table in _catalog._profiles:
        _add(real_table)

    # Stage 2: table-level aliases.
    for canonical, profile in _catalog._profiles.items():
        if real_table in (profile.get("aliases") or []):
            _add(canonical)

    real_norm = _normalize(real_table)
    for canonical in _catalog._profiles:
        if _normalize(canonical) == real_norm:
            _add(canonical)
    for canonical in _catalog._profiles:
        canonical_norm = _normalize(canonical)
        if canonical_norm and (canonical_norm in real_norm or real_norm in canonical_norm):
            _add(canonical)
    return out


_MONTHS: dict[str, int] = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
# also accept 3-letter abbreviations
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")
# Month-year, separator is one of `'`, `-`, or whitespace:
# "October'2024", "October 2024", "Oct'2024", "Oct 2024", "Jan-2024".
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,})\s*[-'\s]\s*(\d{4})$")
# DD-MMM-YYYY: "07-Jul-2024", "7-Jul-2024".
_DAY_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3,})-(\d{4})$")
# ISO datetime (with space or 'T' separator, optional Z / offset / fractional
# seconds): "2024-11-16 10:30:00", "2024-11-16T10:30:00.123Z", "2024-11-16T10:30:00+00:00".
# We only care about the date portion; everything after the first ten chars is dropped.
_ISO_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T][\d:.+\-Z]+$"
)
# ISO date with slash separator, sometimes seen in exports: "2024/11/16".
_ISO_SLASH_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
# US-style slash date: "11/16/2024", "1/7/2024", or 2-digit year "11/16/24".
# We default to MM/DD/YYYY (American Express convention); when the first slot
# is > 12 we re-interpret as DD/MM/YYYY (European fallback).
_US_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
# Numeric dash form "16-11-2024" / "1-7-2024" — same MM/DD vs DD/MM
# disambiguation as the slash form. Distinct from DD-MMM-YYYY because the
# middle group is digits, not letters.
_NUMERIC_DASH_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{2}|\d{4})$")
# Compact ISO basic-format: "20241116" (occasionally produced by data-warehouse
# exports). 8 digits, no separators.
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def _expand_two_digit_year(yy: int) -> int:
    """Expand a 2-digit year to 4 digits with a 50-year sliding window —
    00..49 → 2000..2049, 50..99 → 1950..1999. Banking data spans both
    eras, so a fixed pivot avoids "11/16/24" silently meaning 1924.
    """
    return 2000 + yy if yy < 50 else 1900 + yy


def _date_key(value: Any) -> tuple[int, int, int] | None:
    """Parse common date / period string formats into a comparable
    (year, month, day) tuple. Returns None if unparseable.

    Handles formats produced across the data profiles:
      - ``2025-11-16``                                     → (2025, 11, 16)
      - ``2025-11-16 10:30:00`` / ``2025-11-16T10:30:00Z`` → (2025, 11, 16)
      - ``2025/11/16``                                     → (2025, 11, 16)
      - ``11/16/2025`` / ``11/16/25`` (US, MM/DD/YYYY)     → (2025, 11, 16)
      - ``16/11/2025`` (auto-detected DD/MM when DD > 12)  → (2025, 11, 16)
      - ``11-16-2025`` (US numeric dash, same disambig)    → (2025, 11, 16)
      - ``20251116`` (compact ISO basic)                   → (2025, 11, 16)
      - ``07-Jul-2024`` / ``7-Jul-2024``                   → (2024, 7, 7)
      - ``2025-11``                                        → (2025, 11, 1)
      - ``October'2024`` / ``October 2024`` / ``Oct'2024`` → (2024, 10, 1)
      - ``Jan-2024`` / ``Jan 2024`` / ``January-2024``     → (2024, 1, 1)
      - ``2025``                                           → (2025, 1, 1)

    Slash / numeric-dash forms with all-digits in every slot are inherently
    ambiguous between MM/DD/YYYY (US) and DD/MM/YYYY (EU). We pick MM/DD by
    default (American Express convention) and only flip to DD/MM when the
    first slot exceeds 12. Mixed corpora may still mis-bucket; if you see
    that, consider normalizing upstream at ingestion.

    Tuple comparison matches chronological order for any of these.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    m = _ISO_DATE_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # ISO datetime — drop the time portion. Comes before _ISO_MONTH_RE / etc.
    # because the prefix "YYYY-MM-DD " starts the same as ISO date but won't
    # match the bare-date regex above (which is anchored to end-of-string).
    m = _ISO_DATETIME_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # ISO with slashes — same shape as ISO date but with `/`.
    m = _ISO_SLASH_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD-MMM-YYYY (must come before _ISO_MONTH_RE since both contain hyphens
    # but this one starts with a 1-2 digit day).
    m = _DAY_MONTH_YEAR_RE.match(s)
    if m:
        month_idx = _MONTHS.get(m.group(2).lower())
        if month_idx is not None:
            return (int(m.group(3)), month_idx, int(m.group(1)))

    # US-slash date with MM/DD vs DD/MM auto-disambiguation.
    m = _US_SLASH_RE.match(s)
    if m:
        a, b, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if len(m.group(3)) == 2:
            yr = _expand_two_digit_year(yr)
        if a > 12 and 1 <= b <= 12:
            month, day = b, a   # DD/MM/YYYY — first slot was too big to be a month
        elif 1 <= a <= 12 and 1 <= b <= 31:
            month, day = a, b   # MM/DD/YYYY (default)
        else:
            return None
        return (yr, month, day)

    # Numeric-dash date (same disambiguation rules as US slash).
    m = _NUMERIC_DASH_RE.match(s)
    if m:
        a, b, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if len(m.group(3)) == 2:
            yr = _expand_two_digit_year(yr)
        if a > 12 and 1 <= b <= 12:
            month, day = b, a
        elif 1 <= a <= 12 and 1 <= b <= 31:
            month, day = a, b
        else:
            return None
        return (yr, month, day)

    m = _ISO_MONTH_RE.match(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), 1)

    m = _MONTH_YEAR_RE.match(s)
    if m:
        month_idx = _MONTHS.get(m.group(1).lower())
        if month_idx is not None:
            return (int(m.group(2)), month_idx, 1)

    # Compact ISO "YYYYMMDD". Place AFTER _YEAR_RE would mis-route 4-digit
    # input, so guard with a length check; before _YEAR_RE it would never be
    # reached because that regex matches 4 digits exactly. We check length
    # explicitly here.
    if len(s) == 8 and s.isdigit():
        m = _COMPACT_DATE_RE.match(s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return (y, mo, d)

    m = _YEAR_RE.match(s)
    if m:
        return (int(m.group(1)), 1, 1)

    return None


def _coerce_pair(a: Any, b: Any) -> tuple[Any, Any]:
    """Best-effort comparable coercion: numeric → date-tuple → string.

    Ensures ISO dates, YYYY-MM month strings, plain years, and the
    ``MonthName'YYYY`` format all compare chronologically. For everything
    else, falls back to string comparison.
    """
    # 1) numeric
    try:
        return float(a), float(b)
    except (TypeError, ValueError):
        pass
    # 2) date-ish — only if BOTH sides parse, so a mixed pair doesn't
    #    quietly mis-compare.
    ak, bk = _date_key(a), _date_key(b)
    if ak is not None and bk is not None:
        return ak, bk
    # 3) string fallback
    return (str(a) if a is not None else ""), (str(b) if b is not None else "")


def render_catalog_tree(
    *,
    gateway: DataGateway | None = None,
    catalog: DataCatalog | None = None,
    show_orphans: bool = True,
    max_col_desc: int = 70,
    max_cols_per_table: int | None = None,
) -> str:
    """Render the data catalog as a Unicode tree, grounded in the active case.

    Each branch is one canonical table (with the real CSV name and row count
    annotated). Sub-branches are columns with their dtype and a one-line
    description. Tables in the case but not in the catalog appear under
    "Real-only tables"; tables in the catalog but not in this case appear
    under "In catalog but not in this case".

    ``gateway`` and ``catalog`` default to the module-level state set by
    ``init_tools(...)``. Pass them explicitly to bypass module state when
    debugging from a notebook (e.g. after a fresh kernel).

    Use from a notebook cell::

        from tools.data_tools import render_catalog_tree
        print(render_catalog_tree())                              # uses module state
        print(render_catalog_tree(gateway=gw, catalog=catalog))   # explicit

    Pass ``max_cols_per_table=10`` to truncate wide tables (model_scores has
    50+ cols).
    """
    gw_use = gateway if gateway is not None else _gateway
    cat_use = catalog if catalog is not None else _catalog

    if gw_use is None or cat_use is None:
        return (
            "Data catalog not initialized.\n"
            "Either call tools.data_tools.init_tools(gateway, catalog) first,\n"
            "or pass them explicitly: render_catalog_tree(gateway=gw, catalog=catalog)."
        )

    case_id = gw_use.get_case_id()
    if case_id is None:
        avail = gw_use.list_case_ids()
        avail_hint = (
            f"Available case IDs: {avail[:5]}{'…' if len(avail) > 5 else ''}"
            if avail else "(no cases loaded — check the data-source path)"
        )
        return (
            "No case is active on the gateway — `gw.get_case_id()` returned None.\n"
            "Run the case-selector cell (`gw.set_case(case_id)`) before calling "
            "render_catalog_tree.\n"
            f"{avail_hint}\n\n"
            f"Catalog has {len(cat_use.list_tables())} canonical tables loaded: "
            f"{', '.join(cat_use.list_tables())}"
        )

    real_tables = gw_use.list_tables()
    catalog_tables = cat_use.list_tables()

    # real CSV name → canonical name (via declared aliases or self-match).
    real_to_canonical: dict[str, str] = {}
    for ct in catalog_tables:
        for alias in cat_use.table_aliases(ct):
            real_to_canonical.setdefault(alias, ct)
        real_to_canonical.setdefault(ct, ct)

    # Group real tables by their canonical so multiple aliases (e.g. payments
    # rbinds payments_success + payments_returns) cluster together.
    by_canonical: dict[str, list[str]] = {}
    real_only: list[str] = []
    for real in sorted(real_tables):
        canonical = real_to_canonical.get(real)
        if canonical is None:
            real_only.append(real)
        else:
            by_canonical.setdefault(canonical, []).append(real)

    lines: list[str] = []
    lines.append(f"data_catalog  (case {case_id})")

    canonical_keys = sorted(by_canonical.keys())
    for i, canonical in enumerate(canonical_keys):
        is_last_table = (i == len(canonical_keys) - 1) and not real_only
        t_branch = "└── " if is_last_table else "├── "
        t_spacer = "    " if is_last_table else "│   "

        reals = by_canonical[canonical]
        # First line: canonical ↔ real(s) + total row count
        total_rows = sum(len(gw_use.query(r) or []) for r in reals)
        if len(reals) == 1 and reals[0] == canonical:
            head = f"{canonical}  ({total_rows:,} rows)"
        else:
            head = f"{canonical}  ↔  {', '.join(reals)}  ({total_rows:,} rows)"
        lines.append(f"{t_branch}{head}")

        # Description (one line)
        desc = (cat_use.get_description(canonical) or "").strip().split("\n")[0]
        if desc:
            lines.append(f"{t_spacer}    {desc[:120]}")

        # Columns — taken from the first real table's first row (real headers)
        # plus catalog specs (matched via canonical or declared alias).
        sample_rows = []
        for r in reals:
            rs = gw_use.query(r) or []
            if rs:
                sample_rows = rs
                break
        cols = list(sample_rows[0].keys()) if sample_rows else []
        if max_cols_per_table is not None and len(cols) > max_cols_per_table:
            shown_cols = cols[:max_cols_per_table]
            truncated = len(cols) - max_cols_per_table
        else:
            shown_cols = cols
            truncated = 0

        canonical_cols = ((cat_use._profiles.get(canonical) or {}).get("columns") or {})
        for j, col in enumerate(shown_cols):
            is_last_col = (j == len(shown_cols) - 1) and truncated == 0
            c_branch = "└── " if is_last_col else "├── "
            spec = canonical_cols.get(col)
            if spec is None:
                # alias / normalized fallback
                for cname, cspec in canonical_cols.items():
                    aliases = cspec.get("aliases") or []
                    if col in aliases or _normalize(col) == _normalize(cname):
                        spec = cspec
                        break
            if spec is not None:
                dtype = spec.get("dtype", "?")
                cdesc = (spec.get("description") or "").strip().split("\n")[0]
                if cdesc and len(cdesc) > max_col_desc:
                    cdesc = cdesc[:max_col_desc - 1].rstrip() + "…"
                annot = f"[{dtype}]"
                lines.append(
                    f"{t_spacer}{c_branch}{col}  {annot}"
                    + (f"  — {cdesc}" if cdesc else "")
                )
            else:
                lines.append(f"{t_spacer}{c_branch}{col}  [not in catalog]")

        if truncated > 0:
            lines.append(f"{t_spacer}└── … and {truncated} more column(s)")

    if real_only:
        lines.append("├── Real-only tables (no canonical match):")
        for r in real_only:
            n = len(gw_use.query(r) or [])
            lines.append(f"│   • {r}  ({n:,} rows)")

    if show_orphans:
        in_case = set(by_canonical.keys())
        catalog_only = [ct for ct in catalog_tables if ct not in in_case]
        if catalog_only:
            lines.append("└── In catalog but not in this case: "
                         + ", ".join(sorted(catalog_only)))

    return "\n".join(lines)


def _resolve_real_table(requested: str) -> str:
    """Resolve a requested table name to whatever the gateway actually carries.

    Specialists call query_table with canonical names from skill data_hints
    (e.g. ``crossbu_cards``) but real CSVs may use a slightly different name
    (``crossbu_cards_data``). Resolution order:

      1. Gateway exact match.
      2. Catalog table-level aliases (canonical → real).
      3. ``<requested>_data`` convention — many real CSVs follow this without
         needing an explicit alias declaration in the profile.
      4. ``<requested>`` matches when stripping the trailing ``_data`` from
         a real table name.
      5. Normalized fuzzy match (case + punctuation only).

    Falls through unchanged when nothing matches.
    """
    if _gateway is None or not requested:
        return requested
    real_tables = _gateway.list_tables() if _gateway.get_case_id() else []
    if not real_tables:
        return requested
    if requested in real_tables:
        return requested

    # Canonical → real via catalog's declared table-level aliases.
    if _catalog is not None:
        aliases = _catalog.table_aliases(requested)
        for alias in aliases:
            if alias in real_tables:
                return alias

    # `<canonical>_data` convention (e.g. spends → spends_data, bureau →
    # bureau_data). Cheap, generic; works for any profile without needing an
    # alias declaration.
    candidate = f"{requested}_data"
    if candidate in real_tables:
        return candidate

    # Reverse direction — caller might pass the `_data` form when only the
    # base canonical exists (rare but cheap to check).
    if requested.endswith("_data"):
        base = requested[:-len("_data")]
        if base in real_tables:
            return base

    # Normalized fuzzy fallback.
    target = _normalize(requested)
    if not target:
        return requested
    for real in real_tables:
        if _normalize(real) == target:
            return real
    return requested


def _resolve_real_column(
    rows: list[dict],
    requested: str,
    table_name: str | None = None,
) -> str:
    """Resolve a requested column name to the actual key used in rows.

    Lookup order:
      1. Exact match in the row's real keys.
      2. Catalog-declared aliases — most authoritative; ``payments.yaml``
         declares e.g. ``return_flag.aliases: [Return Flag]`` so a specialist
         passing the canonical ``return_flag`` resolves to ``Return Flag`` in
         the real CSV.
      3. Normalization-based fuzzy match (case + punctuation only) as a
         fallback for variants the catalog hasn't declared.

    Falls through (returns the input) when nothing matches — the filter or
    projection will then return zero rows / drop the column, which is the
    right behavior for a genuinely-missing column.
    """
    if not rows or not requested:
        return requested
    real_keys = list(rows[0].keys())
    if requested in real_keys:
        return requested

    if _catalog is not None and table_name:
        canonical_table = _resolve_canonical_table(table_name) or table_name
        resolved = _catalog.resolve_real_column(canonical_table, requested, real_keys)
        if resolved != requested and resolved in real_keys:
            return resolved

    target = _normalize(requested)
    if not target:
        return requested
    for k in real_keys:
        if _normalize(k) == target:
            return k
    return requested


def _apply_filter(
    rows: list[dict],
    column: str,
    value: str,
    op: str,
) -> list[dict]:
    """Filter rows by column using the named comparison operator.

    Supported ops: eq, ne, gt, gte, lt, lte, between.
    For ``between``, ``value`` must be "<low>,<high>" (inclusive bounds).
    """
    op = (op or "eq").lower()
    if op == "between":
        parts = [v.strip() for v in str(value).split(",") if v.strip()]
        if len(parts) != 2:
            return rows
        lo, hi = parts
        out: list[dict] = []
        for r in rows:
            cell = r.get(column)
            if cell is None:
                continue
            a_lo, b_lo = _coerce_pair(cell, lo)
            a_hi, b_hi = _coerce_pair(cell, hi)
            if a_lo >= b_lo and a_hi <= b_hi:
                out.append(r)
        return out

    cmp = _FILTER_OPS.get(op)
    if cmp is None:
        return rows
    out = []
    for r in rows:
        cell = r.get(column)
        if cell is None:
            continue
        a, b = _coerce_pair(cell, value)
        if cmp(a, b):
            out.append(r)
    return out


def init_tools(gateway: DataGateway, catalog: DataCatalog, logger: Any = None) -> None:
    """Initialize the module-level tool state.

    ``logger`` is optional; when provided (typically an ``EventLogger``),
    every tool invocation emits a ``tool_call`` event (with args) and a
    ``tool_result`` event (with row count + preview of the returned string)
    so the data pipeline is visible in the session log.

    Resets the schema cache too — a re-init typically means a different
    gateway / catalog, so cached schemas from the previous wiring are
    no longer valid.
    """
    global _gateway, _catalog, _logger
    _gateway = gateway
    _catalog = catalog
    _logger = logger
    _schema_cache.clear()


def clear_schema_cache() -> None:
    """Drop all memoized ``get_table_schema`` results. Call this whenever
    the catalog or a case's gateway state changes mid-session (e.g. after a
    ``datalayer.adapter.apply_diff_in_memory`` mutation that adds new
    columns / aliases). Idempotent.
    """
    _schema_cache.clear()


def set_logger(logger: Any) -> None:
    """Attach (or detach) a logger after ``init_tools`` has been called.

    Useful in notebooks where data is loaded before the session logger is
    constructed. Pass ``None`` to silence logging.
    """
    global _logger
    _logger = logger


def _log_call(tool: str, args: dict[str, Any]) -> None:
    if _logger is not None:
        _logger.log("tool_call", {"tool": tool, "args": args})


def _log_result(
    tool: str,
    *,
    result: str,
    rows_returned: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if _logger is None:
        return
    preview = result if len(result) <= _LOG_PREVIEW_CHARS else result[:_LOG_PREVIEW_CHARS] + "…"
    payload: dict[str, Any] = {
        "tool": tool,
        "result_preview": preview,
        "result_chars": len(result),
    }
    if rows_returned is not None:
        payload["rows_returned"] = rows_returned
    if extra:
        payload.update(extra)
    _logger.log("tool_result", payload)


def _list_available_tables_impl() -> str:
    """List all data tables available for the current case, each with its description."""
    _log_call("list_available_tables", {})
    if _catalog is None:
        out = "Data unavailable"
        _log_result("list_available_tables", result=out)
        return out

    def _render(tables: list[str]) -> str:
        lines: list[str] = []
        for t in tables:
            canonical = _resolve_canonical_table(t) or t
            desc = _catalog.get_description(canonical) if _catalog else ""
            label = (
                f"{t} [canonical: {canonical}]"
                if canonical != t and desc
                else t
            )
            if desc:
                lines.append(f"- {label}: {desc}")
            else:
                lines.append(f"- {label}")
        return "\n".join(lines)

    if _gateway is not None and _gateway.get_case_id() is not None:
        case_tables = _gateway.list_tables()
        if case_tables:
            out = "Tables for the current case:\n" + _render(case_tables)
            _log_result("list_available_tables", result=out,
                        extra={"table_count": len(case_tables)})
            return out
        out = "No tables available for the current case."
        _log_result("list_available_tables", result=out,
                    extra={"table_count": 0})
        return out

    tables = _catalog.list_tables()
    out = _render(tables) if tables else "No tables available"
    _log_result("list_available_tables", result=out,
                extra={"table_count": len(tables)})
    return out


@function_tool
def list_available_tables() -> str:
    """List all data tables available for the current case, each with its description."""
    return _list_available_tables_impl()


def _get_table_schema_impl(table_name: str) -> str:
    """Get the column schema for a specific table.

    When a case is active, the schema is filtered to only the columns
    physically present in the case's CSV (i.e., the simulated catalog's
    extra columns are hidden). Each real column is annotated with the
    canonical column's dtype + description if a match exists in the
    canonical profile (via name, alias, or normalized fuzzy match).
    Columns present in the CSV but absent from the canonical are emitted
    with ``type: unknown`` and a "(not in catalog)" description so the
    LLM still sees they exist.

    Memoized per ``(case_id, table_name)`` in ``_schema_cache``. Multiple
    specialists probing the same table within a turn — or repeat probes
    across turns within the same case session — hit the cache instead of
    walking the catalog + gateway again. The result is deterministic per
    case (catalog + gateway state are stable post-first-open), so the
    cache never goes stale within a session. ``init_tools`` resets it.
    """
    _log_call("get_table_schema", {"table_name": table_name})
    case_id = _gateway.get_case_id() if _gateway is not None else None
    cache_key = (case_id, table_name)
    if cache_key in _schema_cache:
        out = _schema_cache[cache_key]
        _log_result("get_table_schema", result=out,
                    extra={"table_name": table_name, "cache_hit": True,
                           "case_id_present": case_id is not None})
        return out

    def _store(out_str: str, extra: dict | None = None) -> str:
        _schema_cache[cache_key] = out_str
        _log_result("get_table_schema", result=out_str,
                    extra={**(extra or {}), "cache_hit": False})
        return out_str

    if _catalog is None:
        return _store("Data unavailable")

    if _gateway is not None and _gateway.get_case_id() is not None:
        # Resolve canonical → real table name (specialists may pass either).
        real_table = _resolve_real_table(table_name)
        rows = _gateway.query(real_table) or []
        if not rows:
            return _store(
                f"Data unavailable: table '{table_name}' not found for current case.",
                extra={"table_name": table_name, "found": False},
            )
        if real_table != table_name:
            table_name = real_table

        canonical_tables = _resolve_canonical_tables(table_name)
        # Build a merged column-spec map across all matching canonical tables.
        # Earlier entries win, so a hand-written real-data profile takes
        # precedence over the broader canonical it shares a name with.
        merged_cols: dict[str, dict] = {}
        canonical_lookup: dict[str, str] = {}  # col_name → canonical name
        for ct in canonical_tables:
            for col, spec in (_catalog._profiles.get(ct, {}).get("columns", {}) or {}).items():
                merged_cols.setdefault(col, spec)
                canonical_lookup.setdefault(col, col)

        # Add table-level aliases preface so the LLM sees the table is the
        # rbind of multiple sources when applicable.
        table_aliases: list[str] = []
        for ct in canonical_tables:
            table_aliases.extend(_catalog.table_aliases(ct))

        schema: dict[str, dict] = {}
        for real_col in rows[0].keys():
            spec = _find_column_spec(merged_cols, real_col)
            if spec is not None:
                # Determine the canonical name for this real column.
                if real_col in merged_cols:
                    canonical = real_col
                else:
                    canonical = next(
                        (c for c, s in merged_cols.items()
                         if real_col in (s.get("aliases") or [])
                         or _normalize(c) == _normalize(real_col)
                         or any(_normalize(a) == _normalize(real_col)
                                for a in (s.get("aliases") or []))),
                        real_col,
                    )
                entry: dict = {
                    "type": spec.get("dtype", "unknown"),
                    "description": spec.get("description", ""),
                }
                if canonical != real_col:
                    entry["canonical_name"] = canonical
                aliases = spec.get("aliases") or []
                if aliases:
                    entry["aliases"] = list(aliases)
                # Surface declared categorical values when the profile has
                # them — helps specialists pick the right filter_value
                # vocabulary. NOTE: these are example/reference values from
                # the catalog (post-sync they may reflect real-data
                # observation), NOT an authoritative scope for inference.
                # Specialists must probe the actual data when in doubt; see
                # the SCHEMA & VOCABULARY DISCIPLINE rules in data_query.md.
                if "categories" in spec:
                    entry["declared_values"] = list(spec["categories"].keys())
                schema[real_col] = entry
            else:
                schema[real_col] = {"type": "unknown", "description": "(not in catalog)"}

        if table_aliases:
            schema["__table_aliases__"] = table_aliases

        return _store(
            json.dumps(schema, indent=2),
            extra={"table_name": table_name, "found": True,
                   "canonical": canonical_tables[0] if canonical_tables else None,
                   "canonical_chain": canonical_tables,
                   "column_count": len(schema)},
        )

    schema = _catalog.get_schema(table_name)
    if schema is None:
        return _store(
            "Data unavailable",
            extra={"table_name": table_name, "found": False},
        )
    return _store(
        json.dumps(schema, indent=2),
        extra={"table_name": table_name, "found": True,
               "column_count": len(schema)},
    )


@function_tool
def get_table_schema(table_name: str) -> str:
    """Get the column schema for a specific table.

    When a case is active, the schema is filtered to only the columns
    physically present in the case's CSV. Each real column is annotated with
    the canonical column's dtype + description if a match exists in the
    canonical profile.
    """
    return _get_table_schema_impl(table_name)


def _find_column_spec(canonical_cols: dict, real_col: str) -> dict | None:
    """Return the canonical spec matching a real column name (or None).

    Checks: exact key, alias list, normalized form across both.
    """
    if real_col in canonical_cols:
        return canonical_cols[real_col]
    real_norm = _normalize(real_col)
    for spec in canonical_cols.values():
        if real_col in (spec.get("aliases") or []):
            return spec
    for canonical_col, spec in canonical_cols.items():
        if _normalize(canonical_col) == real_norm:
            return spec
        for alias in spec.get("aliases") or []:
            if _normalize(alias) == real_norm:
                return spec
    return None


def _query_table_impl(
    table_name: str,
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    columns: str = "",
) -> str:
    """Query a data table for the current case. All data is scoped to the active case.

    Returns a JSON object with structured count metadata and a sample of rows:
        {
          "table": "<name>",
          "filter": "<col> <op> <value>" or null,
          "columns_requested": [...] or null,
          "total_rows_in_table": int,    # rows in the table for this case
          "rows_matching_filter": int,   # rows after filter — the TRUE count
          "rows_returned": int,          # rows actually included in `rows` (may be truncated)
          "truncated": bool,
          "truncation_note": "showing 4/186 rows, 6/12 columns" (only if truncated),
          "rows": [ {...}, ... ]
        }

    For "how many" questions: ALWAYS read `rows_matching_filter`. NEVER count
    the entries in the `rows` array — that array is a display sample and may
    be truncated when the table is large or rows are wide. The sample lets
    you verify shape and pick representative values; the count comes from
    `rows_matching_filter`.

    Args:
        table_name: the table to query.
        filter_column: column to filter on (optional).
        filter_value: value(s) for the filter. For ``filter_op="between"`` pass
            "<low>,<high>" (inclusive). For ISO dates (YYYY-MM-DD) and YYYY-MM
            strings, lexicographic order matches chronological order.
        filter_op: one of "eq" (default), "ne", "gt", "gte", "lt", "lte", "between".
            Use range ops for time windows — e.g. for payments in the 3 months
            before cut-off 2025-12-01, call:
                query_table("payments", filter_column="payment_date",
                            filter_op="gte", filter_value="2025-09-01")
            Or use "between" to bound both sides.
        columns: comma-separated list of column names to return (e.g.
            "fico_score,derog_count"). Leave empty to return all columns.
            REQUIRED for wide tables like model_scores (266 cols) to avoid
            slow processing — request only the columns you need.
    """
    _log_call("query_table", {
        "table_name": table_name,
        "filter_column": filter_column,
        "filter_value": filter_value,
        "filter_op": filter_op if (filter_column and filter_value) else None,
        "columns": columns,
    })

    if _gateway is None:
        out = (
            "Data unavailable: data layer is not initialized for this session "
            "(no gateway is bound to tools.data_tools). This is an infrastructure "
            "error, NOT a finding about the case data — do not interpret it as "
            "'no data exists'. In a notebook, re-run the cell that calls "
            "init_tools(gateway, catalog) and gateway.set_case(case_id)."
        )
        _log_result("query_table", result=out,
                    extra={"reason": "no_gateway_bound"})
        return out

    # Fetch ALL rows for this case, then apply the filter in Python so we
    # can support range operators. The gateway itself only knows exact match.
    # Resolve canonical → real table name (e.g. 'crossbu_cards' →
    # 'crossbu_cards_data') so specialists can call with either name.
    real_table = _resolve_real_table(table_name)
    rows = _gateway.query(real_table, filters=None)
    if rows is None:
        out = f"Data unavailable: table '{table_name}' not found for current case."
        _log_result("query_table", result=out,
                    extra={"table_name": table_name, "found": False})
        return out
    # Use the resolved name in the response so the LLM sees the actual table.
    if real_table != table_name:
        table_name = real_table

    total_rows_in_table = len(rows)
    filter_descriptor: str | None = None
    resolved_filter_column: str | None = None
    if filter_column and filter_value:
        # Resolve case/space variants ('return_flag' → 'Return Flag') against
        # the real CSV headers before filtering. Without this, a specialist
        # following the skill's snake_case names silently gets 0 rows.
        resolved_filter_column = _resolve_real_column(rows, filter_column, table_name)
        rows = _apply_filter(rows, resolved_filter_column, str(filter_value), filter_op)
        if resolved_filter_column != filter_column:
            filter_descriptor = (
                f"{resolved_filter_column} {filter_op} {filter_value!r} "
                f"(resolved from '{filter_column}')"
            )
        else:
            filter_descriptor = f"{filter_column} {filter_op} {filter_value!r}"
    rows_matching_filter = len(rows)

    # Column projection — select only requested columns (with the same
    # case/space resolution as the filter column).
    requested_cols: list[str] | None = None
    if columns:
        requested = [c.strip() for c in columns.split(",") if c.strip()]
        if requested:
            requested_cols = requested
            if rows:
                resolved_map = {c: _resolve_real_column(rows, c, table_name) for c in requested}
                rows = [
                    {resolved_map[c]: row[resolved_map[c]]
                     for c in requested if resolved_map[c] in row}
                    for row in rows
                ]

    truncation_notes: list[str] = []

    if rows:
        total_cols = len(rows[0])
        # Step 1: trim columns if a single row is already too wide
        single_row_size = len(json.dumps([rows[0]], indent=2, default=str))
        if single_row_size > _MAX_CHARS - 600:
            keys = list(rows[0].keys())
            keep_keys: list[str] = []
            for k in keys:
                test_row = {kk: rows[0][kk] for kk in keep_keys + [k]}
                if len(json.dumps([test_row], indent=2, default=str)) > _MAX_CHARS - 700:
                    break
                keep_keys.append(k)
            rows = [{k: row[k] for k in keep_keys if k in row} for row in rows]
            truncation_notes.append(f"showing {len(keep_keys)}/{total_cols} columns")

        # Step 2: reduce rows until JSON fits, leaving room for the wrapper
        text = json.dumps(rows, indent=2, default=str)
        while len(text) > _MAX_CHARS - 500 and len(rows) > 1:
            rows = rows[: len(rows) // 2]
            text = json.dumps(rows, indent=2, default=str)
        if len(rows) < rows_matching_filter:
            truncation_notes.append(f"showing {len(rows)}/{rows_matching_filter} rows")

    rows_returned = len(rows)
    truncated = bool(truncation_notes)

    response: dict[str, Any] = {
        "table": table_name,
        "filter": filter_descriptor,
        "columns_requested": requested_cols,
        "total_rows_in_table": total_rows_in_table,
        "rows_matching_filter": rows_matching_filter,
        "rows_returned": rows_returned,
        "truncated": truncated,
        "rows": rows,
    }
    if truncated:
        response["truncation_note"] = ", ".join(truncation_notes)
        response["count_advice"] = (
            "rows_matching_filter is the true count; the rows array below is a "
            "display sample — do NOT count its entries for 'how many' questions."
        )

    out = json.dumps(response, indent=2, default=str)
    _log_result(
        "query_table", result=out, rows_returned=rows_returned,
        extra={
            "table_name": table_name,
            "rows_before_filter": total_rows_in_table,
            "rows_after_filter": rows_matching_filter,
            "rows_shown": rows_returned,
            "truncation": truncation_notes or None,
        },
    )
    return out


@function_tool
def query_table(
    table_name: str,
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    columns: str = "",
) -> str:
    """Query a data table for the current case. All data is scoped to the active case.

    Returns a JSON object: {table, filter, total_rows_in_table,
    rows_matching_filter, rows_returned, truncated, rows: [...]}.

    For 'how many' / count questions: ALWAYS read `rows_matching_filter` from
    the response. The `rows` array is a display sample that may be truncated
    when the table is large — counting its entries gives the wrong answer.

    Args:
        table_name: the table to query.
        filter_column: column to filter on (optional).
        filter_value: value(s) for the filter. For ``filter_op="between"`` pass
            "<low>,<high>" (inclusive). For ISO dates (YYYY-MM-DD) and YYYY-MM
            strings, lexicographic order matches chronological order.
        filter_op: one of "eq" (default), "ne", "gt", "gte", "lt", "lte", "between".
        columns: comma-separated list of column names to return (e.g.
            "fico_score,derog_count"). Leave empty to return all columns.
    """
    return _query_table_impl(
        table_name=table_name,
        filter_column=filter_column,
        filter_value=filter_value,
        filter_op=filter_op,
        columns=columns,
    )


# ── aggregate_column ──────────────────────────────────────────────────────
#
# Server-side aggregation tool. The redaction layer masks any 6+ digit run
# (`\d{6,}`) — so when an LLM tries to compose an answer like "the total
# balance is $174897.36", the boundary redact_payload turns it into
# "***MASKED***.36" because `174897` is six digits. Computing the aggregate
# in Python and formatting with thousand-separators ($174,897.36) sidesteps
# the regex (commas break the digit run) so the value survives unchanged
# through every redaction boundary. Specialists must use this tool for any
# total / mean / max / min / count question instead of summing rows mentally.

_MONEY_KEY = ("balance", "amount", "limit", "spend", "payment", "value", "exposure")


def _looks_like_money(column: str) -> bool:
    c = (column or "").lower()
    return any(k in c for k in _MONEY_KEY)


def _format_aggregate(value, column: str, op: str) -> str:
    """Format an aggregate result so it survives 6+ digit redaction.

    Always uses thousand separators. Prepends '$' for monetary-looking
    columns. Counts are integer; sums/means/max/min on monetary columns
    show two decimal places.
    """
    if value is None:
        return "(no data)"
    is_money = _looks_like_money(column) and op != "count"
    if op == "count" or (isinstance(value, (int, float)) and float(value).is_integer()
                         and not is_money):
        formatted = f"{int(value):,}"
    else:
        formatted = f"{value:,.2f}"
    return f"${formatted}" if is_money else formatted


def _aggregate_column_impl(
    table_name: str,
    column: str,
    op: str = "sum",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    """Compute an aggregate over a column, returning a formatted string."""
    op = (op or "sum").lower()
    _log_call("aggregate_column", {
        "table_name": table_name, "column": column, "op": op,
        "filter_column": filter_column, "filter_value": filter_value,
        "filter_op": filter_op if (filter_column and filter_value) else None,
    })

    if _gateway is None:
        out = (
            "Data unavailable: data layer is not initialized for this session. "
            "Infrastructure error, not a data finding."
        )
        _log_result("aggregate_column", result=out,
                    extra={"reason": "no_gateway_bound"})
        return out

    real_table = _resolve_real_table(table_name)
    rows = _gateway.query(real_table, filters=None)
    if rows is None:
        out = f"Data unavailable: table '{table_name}' not found for current case."
        _log_result("aggregate_column", result=out,
                    extra={"table_name": table_name, "found": False})
        return out

    total_rows = len(rows)
    filter_descr = ""
    if filter_column and filter_value:
        resolved = _resolve_real_column(rows, filter_column, real_table)
        rows = _apply_filter(rows, resolved, str(filter_value), filter_op)
        filter_descr = (
            f" filtered by {resolved} {filter_op} {filter_value!r}"
            if resolved == filter_column
            else f" filtered by {resolved} (resolved from '{filter_column}') "
                 f"{filter_op} {filter_value!r}"
        )

    n_matching = len(rows)

    # `count` doesn't need the column to be numeric.
    if op == "count":
        result_str = _format_aggregate(n_matching, column, op)
        out = (
            f"count{filter_descr} = {result_str} "
            f"(out of {total_rows:,} total rows in {real_table})"
        )
        _log_result("aggregate_column", result=out,
                    extra={"op": op, "n_matching": n_matching, "total": total_rows})
        return out

    if not rows:
        out = (
            f"{op}({column}){filter_descr} = (no matching rows; "
            f"{total_rows:,} total in {real_table})"
        )
        _log_result("aggregate_column", result=out,
                    extra={"op": op, "n_matching": 0})
        return out

    real_col = _resolve_real_column(rows, column, real_table)
    values: list[float] = []
    skipped = 0
    for r in rows:
        v = r.get(real_col)
        if v is None or v == "":
            skipped += 1
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            skipped += 1

    if not values:
        # Date-aware fallback for max / min: when a column is a date / period
        # string (DD-MMM-YYYY, MonthName'YYYY, etc.) numeric coercion fails.
        # Use _date_key to compare chronologically and return the actual cell
        # string verbatim. This is the right path for "first / last <date col>"
        # questions on payment_date, spend_date, trans_month, etc.
        if op in ("max", "min"):
            dated: list[tuple[tuple, str]] = []
            for r in rows:
                v = r.get(real_col)
                if v is None or v == "":
                    continue
                key = _date_key(v)
                if key is not None:
                    dated.append((key, str(v)))
            if dated:
                if op == "max":
                    _, value_str = max(dated, key=lambda x: x[0])
                    descriptor = "latest date"
                else:
                    _, value_str = min(dated, key=lambda x: x[0])
                    descriptor = "earliest date"
                out = (
                    f"{op}({real_col}){filter_descr} = {value_str!r} "
                    f"({descriptor} among {len(dated):,} non-null value(s) in "
                    f"{n_matching:,} matching row(s); {total_rows:,} total in {real_table})"
                )
                _log_result(
                    "aggregate_column", result=out,
                    extra={
                        "op": op, "column": real_col, "value": value_str,
                        "kind": "date",
                        "n_matching": n_matching, "n_dated": len(dated),
                        "total": total_rows,
                    },
                )
                return out

        out = (
            f"No numeric or date values for column {real_col!r} in "
            f"{n_matching:,} matching row(s). Check column name + dtype."
        )
        _log_result("aggregate_column", result=out,
                    extra={"op": op, "n_matching": n_matching, "skipped": skipped})
        return out

    if op == "sum":
        result = sum(values)
    elif op == "mean" or op == "avg":
        result = sum(values) / len(values)
    elif op == "max":
        result = max(values)
    elif op == "min":
        result = min(values)
    else:
        out = (
            f"Unknown aggregation op {op!r}. Supported: "
            f"sum, mean, max, min, count."
        )
        _log_result("aggregate_column", result=out, extra={"op": op})
        return out

    formatted = _format_aggregate(result, column, op)
    nonnull = len(values)
    out = (
        f"{op}({real_col}){filter_descr} = {formatted} "
        f"(over {nonnull:,} non-null value(s) in {n_matching:,} matching row(s); "
        f"{total_rows:,} total in {real_table})"
    )
    _log_result(
        "aggregate_column", result=out,
        extra={
            "op": op, "column": real_col, "raw_value": result,
            "n_matching": n_matching, "n_nonnull": nonnull,
            "skipped": skipped, "total": total_rows,
        },
    )
    return out


@function_tool
def aggregate_column(
    table_name: str,
    column: str,
    op: str = "sum",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    """Compute an aggregate (sum / mean / max / min / count) over a column.

    Use this for ANY question asking for a total, average, maximum, minimum,
    or count. The result is formatted with thousand separators (e.g.
    '$174,897.36') so it survives the boundary redaction layer that masks
    long digit runs — large aggregates you compute mentally from query_table
    rows would otherwise come back as '***MASKED***'.

    Returns a one-line human-readable string like::
        sum(balance) filtered by Card Portfolio eq 'SBS' = $174,897.36
        (over 1 non-null value in 1 matching row; 3 total in crossbu_cards_data)

    Args:
        table_name: the table to aggregate over (canonical or real name).
        column: the column to aggregate. Must be numeric for sum/mean/max/min.
            Ignored for op='count'.
        op: one of 'sum', 'mean', 'max', 'min', 'count'. Default 'sum'.
        filter_column / filter_value / filter_op: optional row filter, same
            semantics as query_table. When omitted, aggregates over ALL
            rows of the table for the active case.
    """
    return _aggregate_column_impl(
        table_name=table_name,
        column=column,
        op=op,
        filter_column=filter_column,
        filter_value=filter_value,
        filter_op=filter_op,
    )


# ── summarize_trend ──────────────────────────────────────────────────────
#
# Pattern / trajectory tool. Collapses a typical "what is the spending
# pattern / payment trajectory / score evolution" investigation — which
# otherwise costs one tool call per period bucket — into a single call
# that returns the per-period series plus summary statistics. Numeric
# only: trend characterization (rising / spiky / etc.) is left to the
# specialist's prompt, which knows the domain thresholds.

_PERIOD_LABELS = ("day", "week", "month", "quarter", "year")


def _bucket_key(date_tuple: tuple[int, int, int], period: str) -> tuple:
    """Map a (year, month, day) tuple to a canonical bucket key for a period."""
    y, m, d = date_tuple
    if period == "day":
        return (y, m, d)
    if period == "week":
        # ISO-week bucketing without importing datetime: approximate via
        # (year, week_of_year). Use Python's stdlib for correctness.
        from datetime import date
        try:
            iso = date(y, m, d).isocalendar()
            return (iso[0], iso[1])  # (iso_year, iso_week)
        except ValueError:
            return (y, m, d)
    if period == "month":
        return (y, m)
    if period == "quarter":
        return (y, (m - 1) // 3 + 1)
    if period == "year":
        return (y,)
    return (y, m)  # fallback: month


def _bucket_label(key: tuple, period: str) -> str:
    """Human-readable bucket label."""
    if period == "day":
        return f"{key[0]:04d}-{key[1]:02d}-{key[2]:02d}"
    if period == "week":
        return f"{key[0]:04d}-W{key[1]:02d}"
    if period == "month":
        return f"{key[0]:04d}-{key[1]:02d}"
    if period == "quarter":
        return f"{key[0]:04d}-Q{key[1]}"
    if period == "year":
        return f"{key[0]:04d}"
    return str(key)


def _enumerate_periods(start_key: tuple, end_key: tuple, period: str) -> list[tuple]:
    """Enumerate all expected bucket keys between two endpoints (inclusive).

    Used for gap detection. Returns [] when start > end or for unsupported
    periods (we skip enumeration for 'day' / 'week' to avoid huge ranges).
    """
    if start_key > end_key:
        return []
    if period == "year":
        return [(y,) for y in range(start_key[0], end_key[0] + 1)]
    if period == "month":
        out: list[tuple] = []
        y, m = start_key
        ey, em = end_key
        while (y, m) <= (ey, em):
            out.append((y, m))
            m += 1
            if m > 12:
                m = 1
                y += 1
        return out
    if period == "quarter":
        out = []
        y, q = start_key
        ey, eq = end_key
        while (y, q) <= (ey, eq):
            out.append((y, q))
            q += 1
            if q > 4:
                q = 1
                y += 1
        return out
    # day / week: enumeration would be unwieldy for long ranges; report
    # gaps as "n/a" via empty list. Caller must handle.
    return []


def _bucket_value(values: list[float], op: str) -> float:
    if op == "sum":
        return sum(values)
    if op in ("mean", "avg"):
        return sum(values) / len(values)
    if op == "max":
        return max(values)
    if op == "min":
        return min(values)
    if op == "count":
        return float(len(values))
    return sum(values)


def _slope(series: list[tuple[int, float]]) -> float | None:
    """Ordinary least-squares slope of (index, value) — per-bucket change.

    Returns None for fewer than 3 points or zero variance.
    """
    n = len(series)
    if n < 3:
        return None
    xs = [p[0] for p in series]
    ys = [p[1] for p in series]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in series)
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


def _summarize_trend_impl(
    table_name: str,
    value_column: str,
    time_column: str,
    period: str = "month",
    op: str = "sum",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    start_date: str = "",
    end_date: str = "",
) -> str:
    period = (period or "month").lower()
    op = (op or "sum").lower()
    _log_call("summarize_trend", {
        "table_name": table_name,
        "value_column": value_column,
        "time_column": time_column,
        "period": period,
        "op": op,
        "filter_column": filter_column,
        "filter_value": filter_value,
        "filter_op": filter_op if (filter_column and filter_value) else None,
        "start_date": start_date or None,
        "end_date": end_date or None,
    })

    if period not in _PERIOD_LABELS:
        out = (
            f"Unsupported period {period!r}. "
            f"Use one of: {', '.join(_PERIOD_LABELS)}."
        )
        _log_result("summarize_trend", result=out, extra={"reason": "bad_period"})
        return out
    if op not in ("sum", "mean", "avg", "max", "min", "count"):
        out = (
            f"Unsupported op {op!r}. Use one of: sum, mean, max, min, count."
        )
        _log_result("summarize_trend", result=out, extra={"reason": "bad_op"})
        return out

    if _gateway is None:
        out = (
            "Data unavailable: data layer is not initialized for this session. "
            "Infrastructure error, not a data finding."
        )
        _log_result("summarize_trend", result=out,
                    extra={"reason": "no_gateway_bound"})
        return out

    real_table = _resolve_real_table(table_name)
    rows = _gateway.query(real_table, filters=None)
    if rows is None:
        out = f"Data unavailable: table '{table_name}' not found for current case."
        _log_result("summarize_trend", result=out,
                    extra={"table_name": table_name, "found": False})
        return out

    total_rows = len(rows)

    # Optional row filter (e.g. merchant_industry == 'Restaurant').
    filter_descr = ""
    if filter_column and filter_value:
        resolved = _resolve_real_column(rows, filter_column, real_table)
        rows = _apply_filter(rows, resolved, str(filter_value), filter_op)
        filter_descr = (
            f" filtered by {resolved} {filter_op} {filter_value!r}"
            if resolved == filter_column
            else f" filtered by {resolved} (resolved from '{filter_column}') "
                 f"{filter_op} {filter_value!r}"
        )

    if not rows:
        out = (
            f"trend({op}({value_column}) by {period} on {time_column})"
            f"{filter_descr} = (no rows match; {total_rows:,} total in {real_table})"
        )
        _log_result("summarize_trend", result=out,
                    extra={"reason": "no_rows", "n_matching": 0})
        return out

    real_time = _resolve_real_column(rows, time_column, real_table)
    real_value = _resolve_real_column(rows, value_column, real_table)

    # Optional date-range narrowing on the time column.
    start_key = _date_key(start_date) if start_date else None
    end_key = _date_key(end_date) if end_date else None

    # Bucket rows by period.
    buckets: dict[tuple, list[float]] = {}
    n_dated = 0
    n_value_skipped = 0
    n_in_range = 0
    # Track up to 5 distinct unparseable samples so the audit log can surface
    # the actual format _date_key didn't recognize. Without this, the only
    # signal back from a private-env date-format mismatch is the LLM's
    # paraphrased "no parseable values" — useless for diagnosing which
    # format to teach the parser.
    unparseable_samples: list[str] = []
    n_unparseable = 0
    for r in rows:
        t = r.get(real_time)
        dk = _date_key(t)
        if dk is None:
            n_unparseable += 1
            if t is not None and t != "":
                sample = str(t)
                if sample not in unparseable_samples and len(unparseable_samples) < 5:
                    unparseable_samples.append(sample)
            continue
        n_dated += 1
        if start_key is not None and dk < start_key:
            continue
        if end_key is not None and dk > end_key:
            continue
        n_in_range += 1
        if op == "count":
            v: float | None = 1.0
        else:
            raw = r.get(real_value)
            if raw is None or raw == "":
                n_value_skipped += 1
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                n_value_skipped += 1
                continue
        bk = _bucket_key(dk, period)
        buckets.setdefault(bk, []).append(v)

    if not buckets:
        # Surface the actual unrecognized values to the LLM (truncated) so a
        # specialist can decide whether to (a) fall back to a different time
        # column, (b) report a data_gap, or (c) point the reviewer at an
        # ingestion-side fix. Previously the output said only "no parseable
        # values" — opaque, and the LLM tended to hallucinate around it.
        sample_clause = ""
        if unparseable_samples:
            shown = ", ".join(repr(s)[:40] for s in unparseable_samples[:3])
            sample_clause = (
                f"; example unrecognized values: {shown}"
                f" (the parser supports ISO `YYYY-MM-DD`, ISO datetimes, "
                f"`MM/DD/YYYY`, `DD-MMM-YYYY`, and similar — if these are "
                f"valid dates, the format may need to be normalized at "
                f"ingestion)"
            )
        out = (
            f"trend({op}({value_column}) by {period} on {time_column})"
            f"{filter_descr} = (no parseable {time_column} values"
            + (f" in date range {start_date}..{end_date}" if start_key or end_key else "")
            + f"; {total_rows:,} total in {real_table}"
            + f"; {n_unparseable:,} row(s) had unrecognized {time_column} format"
            + sample_clause
            + ")"
        )
        _log_result("summarize_trend", result=out,
                    extra={"reason": "no_buckets",
                           "n_dated": n_dated, "n_in_range": n_in_range,
                           "n_value_skipped": n_value_skipped,
                           "n_unparseable": n_unparseable,
                           "unparseable_samples": unparseable_samples})
        return out

    # Build the per-bucket series in chronological order.
    keys_sorted = sorted(buckets.keys())
    series: list[dict] = []
    for k in keys_sorted:
        vs = buckets[k]
        bv = _bucket_value(vs, op)
        series.append({
            "period": _bucket_label(k, period),
            "value": _format_aggregate(bv, value_column, op),
            "raw_value": round(bv, 4) if isinstance(bv, float) else bv,
            "n_records": len(vs),
        })

    # Summary block.
    raw_values = [s["raw_value"] for s in series]
    n_buckets = len(series)
    total = sum(raw_values)
    mean_v = total / n_buckets
    max_idx = max(range(n_buckets), key=lambda i: raw_values[i])
    min_idx = min(range(n_buckets), key=lambda i: raw_values[i])
    first = series[0]
    last = series[-1]
    peak = series[max_idx]
    trough = series[min_idx]

    # Slope (per-bucket change). Useful as a directional signal for the LLM.
    indexed = [(i, v) for i, v in enumerate(raw_values)]
    slope_v = _slope(indexed)

    # Volatility — coefficient of variation (std / |mean|).
    if mean_v != 0 and n_buckets >= 2:
        var = sum((v - mean_v) ** 2 for v in raw_values) / n_buckets
        std = var ** 0.5
        cv = std / abs(mean_v)
    else:
        cv = None

    # Pct change first → last.
    if first["raw_value"] != 0:
        pct_change = (last["raw_value"] - first["raw_value"]) / abs(first["raw_value"])
    else:
        pct_change = None

    # Gap detection — only meaningful for month / quarter / year.
    expected = _enumerate_periods(keys_sorted[0], keys_sorted[-1], period)
    if expected:
        present = set(keys_sorted)
        missing = [_bucket_label(k, period) for k in expected if k not in present]
    else:
        missing = []  # not enumerated for day / week

    summary = {
        "n_buckets": n_buckets,
        "n_records": sum(s["n_records"] for s in series),
        "first": {"period": first["period"], "value": first["value"]},
        "last":  {"period": last["period"],  "value": last["value"]},
        "peak":  {"period": peak["period"],  "value": peak["value"]},
        "trough":{"period": trough["period"],"value": trough["value"]},
        "total":  _format_aggregate(total, value_column, "sum"),
        "mean_per_bucket": _format_aggregate(mean_v, value_column, "mean"),
        "slope_per_bucket": (
            _format_aggregate(slope_v, value_column, "mean")
            if slope_v is not None else None
        ),
        "pct_change_first_to_last": (
            f"{pct_change * 100:+.1f}%" if pct_change is not None else None
        ),
        "coefficient_of_variation": (
            f"{cv:.2f}" if cv is not None else None
        ),
        "missing_periods": missing,  # empty for day/week or when fully covered
    }

    payload = {
        "table": real_table,
        "period": period,
        "op": op,
        "value_column": real_value,
        "time_column": real_time,
        "filter": filter_descr.strip() or None,
        "rows_in_table": total_rows,
        "rows_dated": n_dated,
        "rows_in_range": n_in_range,
        "rows_value_skipped": n_value_skipped,
        "summary": summary,
        "series": series,
    }

    out = json.dumps(payload, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        # Trim the series tail rather than the summary block — summary is
        # the load-bearing part for the LLM's narrative.
        keep = max(1, n_buckets // 2)
        payload["series"] = series[:keep] + [{"…": f"{n_buckets - keep} more periods truncated"}]
        out = json.dumps(payload, indent=2, default=str)

    _log_result(
        "summarize_trend", result=out,
        extra={
            "table_name": real_table, "period": period, "op": op,
            "n_buckets": n_buckets, "n_records": payload["summary"]["n_records"],
            "first_period": first["period"], "last_period": last["period"],
            "peak_period": peak["period"], "trough_period": trough["period"],
            "missing_count": len(missing),
        },
    )
    return out


@function_tool
def summarize_trend(
    table_name: str,
    value_column: str,
    time_column: str,
    period: str = "month",
    op: str = "sum",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """Summarize a value column over time as a single bucketed series + summary.

    Pattern / trajectory tool — use this for ANY question phrased as
    "what is the X pattern / trend / trajectory / over time / by month"
    instead of looping ``aggregate_column`` per period (which burns the
    specialist's per-call turn budget). One call returns the full
    monthly (or weekly / quarterly / etc.) series plus headline stats:
    first / last / peak / trough buckets, total, mean per bucket,
    per-bucket slope, pct change first→last, coefficient of variation,
    and any missing periods between the first and last observation.

    Numeric only — no qualitative labels ("rising", "spiky"). The
    specialist's prompt is responsible for narrating shape from these
    numbers using its domain thresholds.

    Args:
        table_name: table to scan (canonical or real name).
        value_column: numeric column to aggregate inside each bucket.
            Ignored for op='count'.
        time_column: date / period column used to bucket rows. Common
            values across this codebase: 'Date', 'spend_date',
            'payment_date', 'trans_month'.
        period: bucket size. One of 'day', 'week', 'month', 'quarter',
            'year'. Default 'month'.
        op: per-bucket aggregation. One of 'sum', 'mean', 'max', 'min',
            'count'. Default 'sum'.
        filter_column / filter_value / filter_op: optional row filter
            applied before bucketing (same semantics as query_table).
        start_date / end_date: optional inclusive date narrowing on
            ``time_column``. Accepts the same formats as cell values
            (e.g. '2024-11-01', '01-Nov-2024', 'Nov-2024').

    Returns:
        JSON-formatted text with two top-level blocks: ``summary`` (load-
        bearing headline stats) and ``series`` (the per-bucket entries
        in chronological order). Series may be tail-truncated when the
        full payload would exceed the per-tool size cap.
    """
    return _summarize_trend_impl(
        table_name=table_name,
        value_column=value_column,
        time_column=time_column,
        period=period,
        op=op,
        filter_column=filter_column,
        filter_value=filter_value,
        filter_op=filter_op,
        start_date=start_date,
        end_date=end_date,
    )


# ── summarize_by_group ──────────────────────────────────────────────────
#
# Concentration / ranking tool. Same role as summarize_trend but groups by
# a categorical column (merchant name, industry, payment status, …) instead
# of time. Collapses "top N merchants by spend" / "industry mix" / "payment-
# return reasons" into one call with a concentration summary (HHI + top-N
# shares) so the LLM doesn't have to do per-group math by hand.

_VALID_SORT_BY = ("value", "count", "name")


def _summarize_by_group_impl(
    table_name: str,
    value_column: str,
    group_column: str,
    op: str = "sum",
    top_n: int = 10,
    sort_by: str = "value",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    op = (op or "sum").lower()
    sort_by = (sort_by or "value").lower()
    try:
        top_n_int = int(top_n) if top_n else 10
    except (TypeError, ValueError):
        top_n_int = 10
    if top_n_int <= 0:
        top_n_int = 10

    _log_call("summarize_by_group", {
        "table_name": table_name,
        "value_column": value_column,
        "group_column": group_column,
        "op": op, "top_n": top_n_int, "sort_by": sort_by,
        "filter_column": filter_column,
        "filter_value": filter_value,
        "filter_op": filter_op if (filter_column and filter_value) else None,
    })

    if op not in ("sum", "mean", "avg", "max", "min", "count"):
        out = f"Unsupported op {op!r}. Use one of: sum, mean, max, min, count."
        _log_result("summarize_by_group", result=out, extra={"reason": "bad_op"})
        return out
    if sort_by not in _VALID_SORT_BY:
        out = (
            f"Unsupported sort_by {sort_by!r}. "
            f"Use one of: {', '.join(_VALID_SORT_BY)}."
        )
        _log_result("summarize_by_group", result=out, extra={"reason": "bad_sort_by"})
        return out

    if _gateway is None:
        out = (
            "Data unavailable: data layer is not initialized for this session. "
            "Infrastructure error, not a data finding."
        )
        _log_result("summarize_by_group", result=out,
                    extra={"reason": "no_gateway_bound"})
        return out

    real_table = _resolve_real_table(table_name)
    rows = _gateway.query(real_table, filters=None)
    if rows is None:
        out = f"Data unavailable: table '{table_name}' not found for current case."
        _log_result("summarize_by_group", result=out,
                    extra={"table_name": table_name, "found": False})
        return out

    total_rows = len(rows)
    filter_descr = ""
    if filter_column and filter_value:
        resolved = _resolve_real_column(rows, filter_column, real_table)
        rows = _apply_filter(rows, resolved, str(filter_value), filter_op)
        filter_descr = (
            f" filtered by {resolved} {filter_op} {filter_value!r}"
            if resolved == filter_column
            else f" filtered by {resolved} (resolved from '{filter_column}') "
                 f"{filter_op} {filter_value!r}"
        )

    if not rows:
        out = (
            f"top_groups({op}({value_column}) by {group_column})"
            f"{filter_descr} = (no rows match; {total_rows:,} total in {real_table})"
        )
        _log_result("summarize_by_group", result=out,
                    extra={"reason": "no_rows", "n_matching": 0})
        return out

    real_group = _resolve_real_column(rows, group_column, real_table)
    real_value = _resolve_real_column(rows, value_column, real_table)

    # Bucket rows by the categorical value.
    groups: dict[str, list[float]] = {}
    n_value_skipped = 0
    n_group_null = 0
    for r in rows:
        g = r.get(real_group)
        if g is None or (isinstance(g, str) and not g.strip()):
            n_group_null += 1
            continue
        gkey = str(g)
        if op == "count":
            v: float | None = 1.0
        else:
            raw = r.get(real_value)
            if raw is None or raw == "":
                n_value_skipped += 1
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                n_value_skipped += 1
                continue
        groups.setdefault(gkey, []).append(v)

    if not groups:
        out = (
            f"top_groups({op}({value_column}) by {group_column}){filter_descr} = "
            f"(no parseable values; {total_rows:,} total in {real_table})"
        )
        _log_result("summarize_by_group", result=out,
                    extra={"reason": "no_groups",
                           "n_value_skipped": n_value_skipped,
                           "n_group_null": n_group_null})
        return out

    # Per-group stats.
    raw_per_group: list[tuple[str, float, list[float]]] = []
    for g, values in groups.items():
        bv = _bucket_value(values, op)
        raw_per_group.append((g, bv, values))

    # Sort.
    if sort_by == "name":
        raw_per_group.sort(key=lambda x: x[0])
    elif sort_by == "count":
        raw_per_group.sort(key=lambda x: len(x[2]), reverse=True)
    else:  # value (default)
        raw_per_group.sort(key=lambda x: x[1], reverse=True)

    n_groups_total = len(raw_per_group)
    top = raw_per_group[:top_n_int]

    # HHI / concentration summary uses sum-of-shares (op='sum' or count) for
    # interpretability. For mean/max/min, share math is meaningless, so the
    # concentration block only fires for additive ops.
    additive = op in ("sum", "count")
    total_value = sum(v for _, v, _ in raw_per_group) if additive else None
    if additive and total_value and total_value > 0:
        sorted_values = sorted((v for _, v, _ in raw_per_group), reverse=True)
        shares = [v / total_value for v in sorted_values]
        hhi = sum(s * s for s in shares)  # 0..1; higher = more concentrated
        top1_share = shares[0]
        top3_share = sum(shares[:3])
        top5_share = sum(shares[:5])
        concentration = {
            "total_across_groups": _format_aggregate(total_value, value_column,
                                                     "sum" if op == "sum" else "count"),
            "top1_share": f"{top1_share * 100:.1f}%",
            "top3_share": f"{top3_share * 100:.1f}%",
            "top5_share": f"{top5_share * 100:.1f}%",
            "hhi": f"{hhi:.3f}",  # rule of thumb: >0.25 = highly concentrated
        }
    else:
        concentration = None

    # Per-group payload.
    series: list[dict] = []
    for g, bv, values in top:
        n = len(values)
        sub = {
            "group": g,
            "value": _format_aggregate(bv, value_column, op),
            "raw_value": round(bv, 4) if isinstance(bv, float) else bv,
            "n_records": n,
        }
        # When the op already covers it, don't duplicate. Otherwise add a
        # mini-stats block so the LLM can see shape per group in one shot.
        if op in ("sum", "count"):
            sub["mean"] = _format_aggregate(sum(values) / n, value_column, "mean")
            if op != "max":
                sub["max"] = _format_aggregate(max(values), value_column, "max")
            if op != "min":
                sub["min"] = _format_aggregate(min(values), value_column, "min")
        series.append(sub)

    payload = {
        "table": real_table,
        "group_column": real_group,
        "value_column": real_value,
        "op": op,
        "top_n": top_n_int,
        "sort_by": sort_by,
        "filter": filter_descr.strip() or None,
        "rows_in_table": total_rows,
        "rows_used": sum(len(v) for v in groups.values()),
        "rows_value_skipped": n_value_skipped,
        "rows_group_null": n_group_null,
        "n_groups_total": n_groups_total,
        "n_groups_returned": len(series),
        "concentration": concentration,
        "groups": series,
    }

    out = json.dumps(payload, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        # Drop per-group min/mean/max first (heavier than the headline).
        for sub in payload["groups"]:
            for k in ("mean", "max", "min"):
                sub.pop(k, None)
        out = json.dumps(payload, indent=2, default=str)
        if len(out) > _MAX_CHARS:
            keep = max(1, len(payload["groups"]) // 2)
            payload["groups"] = payload["groups"][:keep] + [
                {"…": f"{len(series) - keep} more groups truncated"}
            ]
            out = json.dumps(payload, indent=2, default=str)

    _log_result(
        "summarize_by_group", result=out,
        extra={
            "table_name": real_table,
            "group_column": real_group, "value_column": real_value,
            "op": op, "n_groups_total": n_groups_total,
            "n_groups_returned": len(series),
            "top1_share": (concentration or {}).get("top1_share"),
            "hhi": (concentration or {}).get("hhi"),
        },
    )
    return out


@function_tool
def summarize_by_group(
    table_name: str,
    value_column: str,
    group_column: str,
    op: str = "sum",
    top_n: int = 10,
    sort_by: str = "value",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    """Rank groups within a categorical column by an aggregate of a value column.

    Concentration / "top-N" tool — use this for ANY question phrased as
    "top merchants / which industries / most common return reasons /
    spread by category" instead of looping ``aggregate_column`` per
    filter value (which is wasteful and burns turn budget). One call
    returns the top-N groups + a concentration summary (top1 / top3 /
    top5 share of total + HHI) so the LLM doesn't have to do share
    math by hand.

    Numeric only — no qualitative labels ("highly concentrated", "spread
    out"). The specialist's prompt narrates concentration shape from
    these numbers using its domain thresholds (rule of thumb:
    HHI > 0.25 = highly concentrated, top1_share > 0.30 = single-name
    dominance).

    Args:
        table_name: table to scan (canonical or real name).
        value_column: numeric column to aggregate within each group.
            Ignored for op='count'.
        group_column: categorical column to group by (e.g. 'Merchant
            Name', 'Merchant Industry', 'card_portfolio',
            'Return Flag', 'Return Reason').
        op: per-group aggregation. One of 'sum', 'mean', 'max', 'min',
            'count'. Default 'sum'.
        top_n: how many top groups to return. Default 10.
        sort_by: ordering. 'value' (default) ranks by the per-group
            aggregate; 'count' by record count; 'name' alphabetical.
        filter_column / filter_value / filter_op: optional row filter
            applied BEFORE grouping (same semantics as query_table).

    Returns:
        JSON-formatted text with two top-level blocks: ``concentration``
        (headline shares + HHI; only present for additive ops sum/count)
        and ``groups`` (per-group entries with value + n_records + mini-
        stats). Pair with ``summarize_trend`` filtered to a specific
        group to get that group's time-series shape.
    """
    return _summarize_by_group_impl(
        table_name=table_name,
        value_column=value_column,
        group_column=group_column,
        op=op,
        top_n=top_n,
        sort_by=sort_by,
        filter_column=filter_column,
        filter_value=filter_value,
        filter_op=filter_op,
    )
