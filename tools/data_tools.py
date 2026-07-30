"""Data-access tool functions for agent tool-calling.

All queries are scoped to the currently active case. The case_id is set on the
gateway at session start — tools don't need to specify it.
"""

from __future__ import annotations

import json
import operator
import re
from datetime import date, timedelta
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

# Per-case column index backing ``search_columns`` — same lifecycle and same
# reset points as ``_schema_cache`` above. See ``_build_search_index``.
try:
    _search_index_cache  # type: ignore[used-before-def]  # noqa: F821
except NameError:
    _search_index_cache: dict[str | None, list[dict]] = {}

_MAX_CHARS = 3000
# A trend `series` is load-bearing — auto_chart parses it to render the plotted
# chart — so it gets a larger budget than generic row dumps, and when it is
# STILL too long it is down-sampled across the full range (see
# `_downsample_trend_series`) rather than truncated at one end. The old shared
# 3000-char cap + head-truncation silently dropped the most-recent months from
# the chart.
_TREND_MAX_CHARS = 8000
# `transaction_detail` returns wide DENORMALIZED rows (timestamp + merchant +
# amount + spend vars + 2 scores + 6 drivers ≈ 16 cols, ~900 chars each). At the
# generic 3000-char cap only ~3 rows survive — useless for "extract the abnormal
# transactions", and if the surviving rows happen to be model-only auths/declines
# (no settled spend), merchant/amount look "missing" and the specialist wrongly
# blames the join. Give it a much larger budget so a real extraction (limit 20-30)
# comes back whole, and sample uniformly (not head-truncate) when it must shrink.
_TXN_DETAIL_MAX_CHARS = 16000
_LOG_PREVIEW_CHARS = 500  # how much of tool output to snapshot in tool_result events

# ── Duplicate-call guard ────────────────────────────────────────────────────
#
# The read tools are DETERMINISTIC within a turn — the case data doesn't change,
# so an identical call returns identical rows. Specialists nonetheless sometimes
# re-issue the EXACT same query several rounds in a row (e.g. an unfiltered
# `query_table` dump, hoping to page or see "more"), burning ~30-45s of LLM
# latency per wasted round. This tracks call signatures within the CURRENT turn
# so the tool can short-circuit an exact repeat with a directive to change the
# query, instead of silently re-dumping the same sample and letting the loop run.
_recent_call_sigs: dict[tuple, int] = {}
_recent_call_turn: Any = None


def _seen_this_turn(tool: str, signature: tuple) -> int:
    """Times this exact (tool, signature) was already seen THIS turn (0 = first).

    Turn-scoped via the node-trace ``TURN_SCOPE`` contextvar; resets whenever the
    turn changes. When NO turn scope is active (unit tests, ad-hoc calls) it is
    INERT and always returns 0 — so it never dedups across tests or across turns
    that lack a scope.
    """
    global _recent_call_turn, _recent_call_sigs
    turn = None
    try:  # lazy import — avoids any load-order coupling with node_trace
        from tools.node_trace.core import current_turn_scope
        ts = current_turn_scope()
        turn = ts.turn_id if ts else None
    except Exception:  # noqa: BLE001
        turn = None
    if turn is None:
        return 0  # no active turn → inert (never a false positive)
    if turn != _recent_call_turn:
        _recent_call_turn = turn
        _recent_call_sigs = {}
    key = (tool, signature)
    n = _recent_call_sigs.get(key, 0)
    _recent_call_sigs[key] = n + 1
    return n


def _even_sample(items: list, max_items: int) -> list:
    """Uniformly sub-sample ``items`` to ``<= max_items``, always keeping the
    FIRST and LAST element and spreading the rest evenly across the list. Used
    both to down-sample a trend series (preserve the full x-range) and to
    truncate a large `query_table` result to a REPRESENTATIVE sample that spans
    the whole match set — instead of the first N rows, which cluster on one
    date/value and get mistaken for the full result. Returns ``items`` unchanged
    when it already fits or ``max_items < 2``."""
    n = len(items)
    if n <= max_items or max_items < 2:
        return items
    idx = sorted({round(i * (n - 1) / (max_items - 1)) for i in range(max_items)})
    return [items[i] for i in idx]

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
# DD-MMM-YY (2-digit year): "7-Jul-24", "16-Jul-24" — the private-env
# `payments.payment_date` format. Same shape as DD-MMM-YYYY but the year
# group is 2 digits, expanded via the shared sliding window.
_DAY_MONTH_2YEAR_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3,})-(\d{2})$")
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
# Same as `_US_SLASH_RE` but with a trailing time component the strategy
# table ships in: "5/28/24 3:03", "2/15/25 14:22:11" (seconds optional).
# We accept the date portion and discard the time — `summarize_trend` /
# `aggregate_column` bucket by day-or-coarser, so HH:MM is sub-resolution.
_US_SLASH_DATETIME_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s+\d{1,2}:\d{2}(?::\d{2})?$"
)
# Numeric dash form "16-11-2024" / "1-7-2024" — same MM/DD vs DD/MM
# disambiguation as the slash form. Distinct from DD-MMM-YYYY because the
# middle group is digits, not letters.
_NUMERIC_DASH_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{2}|\d{4})$")
# Compact ISO basic-format: "20241116" (occasionally produced by data-warehouse
# exports). 8 digits, no separators.
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
# Month + 2-digit year: "Jul-25", "Jul'25", "July 25".
_MONTH_2YEAR_RE = re.compile(r"^([A-Za-z]{3,})\s*[-'\s]\s*(\d{2})$")
# Year + month name: "2025-Jul", "2025 July".
_YEAR_MONTH_NAME_RE = re.compile(r"^(\d{4})\s*[-'\s]\s*([A-Za-z]{3,})$")
# Excel serial date base (serial 1 = 1900-01-01; Excel's 1900 leap bug means
# the usable epoch offset is 1899-12-30).
_EXCEL_EPOCH = date(1899, 12, 30)


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
      - ``7-Jul-24`` / ``16-Jul-24`` (2-digit year)        → (2024, 7, 7)
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

    # DD-MMM-YY — 2-digit-year variant of the above ("7-Jul-24"). Comes after
    # the 4-digit form so "07-Jul-2024" never reaches this branch.
    m = _DAY_MONTH_2YEAR_RE.match(s)
    if m:
        month_idx = _MONTHS.get(m.group(2).lower())
        if month_idx is not None:
            return (_expand_two_digit_year(int(m.group(3))), month_idx, int(m.group(1)))

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

    # US-slash date WITH trailing time ("5/28/24 3:03", "2/15/25 14:22:11").
    # Strategy table ships this format — strip the time and reuse the
    # same MM/DD vs DD/MM disambiguation.
    m = _US_SLASH_DATETIME_RE.match(s)
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

    m = _MONTH_2YEAR_RE.match(s)
    if m:
        month_idx = _MONTHS.get(m.group(1).lower())
        if month_idx is not None:
            return (_expand_two_digit_year(int(m.group(2))), month_idx, 1)

    m = _YEAR_MONTH_NAME_RE.match(s)
    if m:
        month_idx = _MONTHS.get(m.group(2).lower())
        if month_idx is not None:
            return (int(m.group(1)), month_idx, 1)

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

    # Excel serial date: a bare 5-digit integer in the plausible range
    # (~1954..2064). Narrow range so ordinary 5-digit counts aren't misread.
    if s.isdigit() and len(s) == 5:
        serial = int(s)
        if 20000 <= serial <= 60000:
            d = _EXCEL_EPOCH + timedelta(days=serial)
            return (d.year, d.month, d.day)

    return None


# Strict numeric form: a plain integer (no leading zeros beyond a lone "0")
# or decimal. Rejects "007"/zip codes, "1e3" scientific notation, and
# inf/nan so ID/code columns are compared as strings, not silently as floats.
_STRICT_NUMBER_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$|^-?0\.\d+$")


def _is_strict_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        # reject nan / inf (nan != nan; inf is in the sentinel tuple)
        return v == v and v not in (float("inf"), float("-inf"))
    return bool(_STRICT_NUMBER_RE.match(str(v).strip()))


# Datetime WITH a time-of-day component, e.g. "2025-05-14 11:35:35.101",
# "2025-05-14T11:35:35", "2025-05-14 11:35:35". Captures down to the SECOND and
# ignores any fractional seconds / timezone suffix — so two representations of
# the SAME instant at different precision (a spend's millisecond `Timestamp`
# vs the model table's second-precision `txn_date_time`) compare equal.
_DT_SECOND_RE = re.compile(
    r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})"
)


def _second_key(x: Any) -> tuple[int, ...] | None:
    """Return a ``(Y, M, D, h, m, s)`` tuple when ``x`` carries a time-of-day,
    else ``None``. Used to compare timestamps at SECOND precision so an exact-
    timestamp lookup isolates a single transaction. Date-only values (no
    ``HH:MM:SS``) return ``None`` and fall through to ``_date_key`` day grain."""
    if x is None:
        return None
    m = _DT_SECOND_RE.match(str(x))
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def _coerce_pair(a: Any, b: Any) -> tuple[Any, Any]:
    """Best-effort comparable coercion: numeric → second-tuple → date-tuple →
    string.

    Ensures ISO dates, YYYY-MM month strings, plain years, and the
    ``MonthName'YYYY`` format all compare chronologically. For everything
    else, falls back to string comparison.
    """
    # 1) numeric — only when BOTH sides are strictly numeric, so ID/code
    #    columns ("007", zip codes, "1e3") and inf/nan don't get coerced.
    if _is_strict_number(a) and _is_strict_number(b):
        return float(a), float(b)
    # 2) datetime with time-of-day on BOTH sides → compare at SECOND precision
    #    so an exact-timestamp lookup pinpoints one transaction (e.g. joining a
    #    spend's millisecond `Timestamp` to the model table's second-precision
    #    `txn_date_time`). Without this, `_date_key` below collapses BOTH to day
    #    grain and the lookup returns every transaction that day.
    at, bt = _second_key(a), _second_key(b)
    if at is not None and bt is not None:
        return at, bt
    # 3) date-ish (day grain) — only if BOTH sides parse, so a mixed pair
    #    doesn't quietly mis-compare. A date-only bound (e.g. "2024-01-01")
    #    still matches a datetime cell at day grain (intended).
    ak, bk = _date_key(a), _date_key(b)
    if ak is not None and bk is not None:
        return ak, bk
    # 4) string fallback
    return (str(a) if a is not None else ""), (str(b) if b is not None else "")


def _join_key(x: Any) -> tuple | None:
    """Normalize ONE value to a hashable join key, mirroring `_coerce_pair`'s
    coercion order (numeric → second-precision datetime → day-date → text). Two
    representations of the same key (a spend's millisecond `Timestamp` and the
    model table's second-precision `txn_date_time`) normalize identically, so
    they join. Returns ``None`` for null cells (never join)."""
    if x is None:
        return None
    if _is_strict_number(x):
        return ("n", float(x))
    sk = _second_key(x)
    if sk is not None:
        return ("dt", sk)
    dk = _date_key(x)
    if dk is not None:
        return ("d", dk)
    return ("s", str(x).strip().casefold())


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
    matches = [k for k in real_keys if _normalize(k) == target]
    if len(matches) == 1:
        return matches[0]
    # 0 matches → genuinely missing. 2+ → ambiguous (e.g. score_1 / score_2
    # both normalize to "score"); refuse rather than silently bind to the
    # wrong sibling column. Return the literal so the caller gets an honest
    # zero / missing-column result instead of wrong rows.
    return requested


_ZERO_MATCH_SAMPLE_VALUES = 8


def _zero_match_diagnostic(
    unfiltered: list[dict], conditions: list[tuple[str, str, str]],
) -> dict | None:
    """Why a filter matched nothing: does the column exist, and what IS in it.

    A bare `rows_matching_filter: 0` is indistinguishable between "wrong column
    name", "wrong value vocabulary" and "genuinely no such rows" — so the skill
    had to carry rules like "check the column's format FIRST" and "appr_deny_cd
    is 0/1, not the words approved/declined". Those are a memory burden standing
    in for feedback the tool can simply give: report the column's actual values
    and the model self-corrects on the next call.

    Values only, bounded and truncated — this is a vocabulary hint, not a data
    dump, and it goes through the same redaction as any other tool output.
    """
    if not unfiltered or not conditions:
        return None
    real_keys = set(unfiltered[0].keys())
    out: dict = {}
    for col, value, op in conditions:
        if col not in real_keys:
            out[col] = {
                "column_exists": False,
                "hint": (f"'{col}' is not a column of this table — call "
                         f"get_table_schema, or search_columns to find it."),
            }
            continue
        seen: list[str] = []
        for r in unfiltered:
            v = r.get(col)
            if v in (None, ""):
                continue
            s = str(v)[:40]
            if s not in seen:
                seen.append(s)
            if len(seen) > _ZERO_MATCH_SAMPLE_VALUES:
                break
        entry: dict = {
            "column_exists": True,
            "filter_tried": f"{op} {value!r}",
            "values_present": seen[:_ZERO_MATCH_SAMPLE_VALUES],
        }
        if len(seen) > _ZERO_MATCH_SAMPLE_VALUES:
            entry["values_present_note"] = "first few distinct values only"
        if not seen:
            entry["column_is_empty"] = True
            entry["hint"] = (f"'{col}' is present but EMPTY for this case — a "
                             f"DATA GAP, not a filter mistake.")
        out[col] = entry
    return out or None


def _apply_filter(
    rows: list[dict],
    column: str,
    value: str,
    op: str,
) -> list[dict]:
    """Filter rows by column using the named comparison operator.

    Supported ops: eq, ne, gt, gte, lt, lte, between, contains.
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

    if op == "contains":
        # Case-insensitive substring match for free-text entity columns
        # (merchant names, reason codes). Null cells never match.
        needle = str(value).strip().casefold()
        out = []
        for r in rows:
            cell = r.get(column)
            if cell is None:
                continue
            if needle in str(cell).casefold():
                out.append(r)
        return out

    if op == "in":
        # Match ANY of a comma-separated value list — the multi-key form of
        # `eq`, using the same forgiving comparison (numeric coercion,
        # second-precision timestamps, case/space-insensitive text). Lets a
        # specialist look up N transactions' scores in ONE call, e.g.
        # `txn_date_time in "<ts1>,<ts2>,<ts3>"`, instead of N separate queries.
        parts = [v.strip() for v in str(value).split(",") if v.strip()]
        if not parts:
            return rows
        out = []
        for r in rows:
            cell = r.get(column)
            if cell is None:
                continue
            for v in parts:
                a, b = _coerce_pair(cell, v)
                if isinstance(a, str) and isinstance(b, str):
                    a, b = a.strip().casefold(), b.strip().casefold()
                if a == b:
                    out.append(r)
                    break
        return out

    cmp = _FILTER_OPS.get(op)
    if cmp is None:
        return rows
    out = []
    for r in rows:
        cell = r.get(column)
        if cell is None:
            # A null cell is "not equal" to any concrete value, so it
            # satisfies `ne`; for all other ops it is dropped.
            if op == "ne":
                out.append(r)
            continue
        a, b = _coerce_pair(cell, value)
        # Text equality is forgiving: case- and whitespace-insensitive.
        # Numeric / date comparisons are unaffected (they coerce to
        # float / date-tuple before reaching here).
        if op in ("eq", "ne") and isinstance(a, str) and isinstance(b, str):
            a, b = a.strip().casefold(), b.strip().casefold()
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
    _search_index_cache.clear()


def clear_schema_cache() -> None:
    """Drop all memoized ``get_table_schema`` results. Call this whenever
    the catalog or a case's gateway state changes mid-session (e.g. after a
    ``datalayer.adapter.apply_diff_in_memory`` mutation that adds new
    columns / aliases). Idempotent.
    """
    _schema_cache.clear()
    _search_index_cache.clear()


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


# ── search_columns ──────────────────────────────────────────────────────────
#
# Why this exists: a specialist proposes variables from its SKILL, then calls
# get_table_schema to confirm them. Both sides are biased toward what the skill
# already names — so a column the skill never mentions is invisible, even when
# it is the one the user asked for. `model_scores` alone ships ~250 real columns
# against ~56 named in the profile; "how is the internal paydown rate" is
# answered by `last_cycle_cut_revolve_rate` (concept: capacity_paydown), which
# no skill enumerates. This tool searches the case's ACTUAL columns by name,
# alias, concept and description so the specialist can find that column from the
# user's wording instead of from its own vocabulary.

# Pure function words. Deliberately SHORT — domain words that look generic
# ("rate", "score", "customer", "internal") are exactly the discriminating terms
# in this catalog, so stripping them would defeat the search.
_SEARCH_STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "did", "do", "does",
    "for", "from", "has", "have", "how", "i", "in", "is", "it", "its", "me",
    "no", "not", "of", "on", "or", "s", "show", "that", "the", "their",
    "there", "this", "to", "was", "were", "what", "when", "which", "with",
})

# Minimum term length for SUBSTRING matching inside a column name. Below this,
# a term must match a whole name token instead: "no" is a substring of
# `ttl_nonp_inq_ons_grms` and dozens more, so short-term substring matching
# turns any query into a scan of the whole catalog. Abbreviations that matter
# ("rvlv", "dpd") still land — 4-char ones as substrings, shorter ones as
# whole tokens, which is how they actually appear in these names.
_SEARCH_MIN_SUBSTRING_TERM = 4

# The `(N others)` remainder row `summarize_by_group` appends when it truncates
# to top_n. Defined HERE, next to the code that writes it, and read by
# `viz_renderer` — which hides the row from ranking bars but still counts it in
# the total. One definition rather than a regex copied across the boundary;
# `test_data_viz_tools` pins that the writer and the reader agree.
TAIL_GROUP_RE = re.compile(r"^\((\d+) others\)$")


def format_tail_group(n: int) -> str:
    """Label for the remainder row covering `n` groups outside the top-N."""
    return f"({n} others)"


# How many breaching periods to list. The count and the LATEST one carry the
# answer; a full list on a long series would crowd out the trend itself.
# How many breach EPISODES to list. The count and the latest one carry the
# answer; older episodes are context.
_MAX_BREACH_EPISODES = 6


def _column_threshold(real_table: str, real_col: str) -> dict | None:
    """`{"value", "direction"}` for a column's catalog risk threshold, or None.

    Catalog thresholds are keyed by CANONICAL column name while the trend
    payload reports the REAL one (`tot_struct_risk_score` vs
    `tot_struct_risk_score_max` in the real monthly export), so match through
    aliases and normalization the same way `_resolve_real_column` does —
    otherwise every real-data column silently has no threshold.
    """
    if _catalog is None or not real_col:
        return None
    target = _normalize(real_col)
    for ct in _resolve_canonical_tables(real_table):
        thresholds = _catalog.get_thresholds(ct)
        if not thresholds:
            continue
        cols = (_catalog._profiles.get(ct, {}).get("columns", {}) or {})
        for canonical, spec in thresholds.items():
            if canonical == real_col or _normalize(canonical) == target:
                return spec
            aliases = (cols.get(canonical) or {}).get("aliases") or []
            if real_col in aliases or any(_normalize(a) == target for a in aliases):
                return spec
    return None


def _search_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, function words removed."""
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _SEARCH_STOPWORDS]


def _build_search_index(case_id) -> list[dict]:
    """One entry per column physically present in this case's tables.

    Mirrors `_get_table_schema_impl`'s resolution (real table → canonical
    profile(s) → per-column spec) so a name found here is a name the other data
    tools accept verbatim. Columns absent from the catalog are still indexed —
    they exist in the data, and their name alone is often the match.
    """
    if case_id in _search_index_cache:
        return _search_index_cache[case_id]

    entries: list[dict] = []
    if _gateway is not None and _catalog is not None:
        for table in _gateway.list_tables():
            rows = _gateway.query(table) or []
            if not rows:
                continue
            merged_cols: dict[str, dict] = {}
            for ct in _resolve_canonical_tables(table):
                for col, spec in (
                    _catalog._profiles.get(ct, {}).get("columns", {}) or {}
                ).items():
                    merged_cols.setdefault(col, spec)
            for real_col in rows[0].keys():
                spec = _find_column_spec(merged_cols, real_col) or {}
                concepts = sorted(DataCatalog._concept_set(spec))
                threshold = ""
                if "risk_threshold" in spec:
                    sym = ">" if spec.get("risk_direction", "above") == "above" else "<"
                    threshold = f"risky {sym} {spec['risk_threshold']}"
                entries.append({
                    "table": table,
                    "column": real_col,
                    "dtype": spec.get("dtype", "unknown"),
                    "description": (spec.get("description") or "").strip(),
                    "concepts": concepts,
                    "aliases": list(spec.get("aliases") or []),
                    "threshold": threshold,
                    "in_catalog": bool(spec),
                })

    _search_index_cache[case_id] = entries
    return entries


def _score_entry(entry: dict, terms: list[str], query_norm: str) -> int:
    """Relevance of one column to the query terms; 0 means "don't show it".

    Weights encode where a match is most trustworthy: the column NAME and its
    curated `concept` are AUTHORED signals, the free-text description is weaker,
    and matching every term at once is what separates a real hit from incidental
    overlap on a common word.

    The floor matters as much as the ranking. Descriptions are long enough that
    almost any query brushes against a few of them, and a specialist handed 25
    weak matches is no better off than one handed none. So a hit must either
    touch an authored identifier, or account for EVERY term in the query.
    """
    name = entry["column"].lower()
    name_tokens = set(re.findall(r"[a-z0-9]+", name))
    concept_text = " ".join(entry["concepts"]).lower()
    alias_text = " ".join(entry["aliases"]).lower()
    desc = entry["description"].lower()

    if _normalize(entry["column"]) == query_norm:
        return 1000                                  # the user named the column
    if query_norm and query_norm in {_normalize(a) for a in entry["aliases"]}:
        return 900

    score = 0
    matched = 0          # terms matched anywhere
    identifier_hits = 0  # terms matched in name / concept / alias
    for term in terms:
        hit = ident = False
        if term in name_tokens:
            score += 6                               # whole word in the name
            hit = ident = True
        elif len(term) >= _SEARCH_MIN_SUBSTRING_TERM and term in name:
            score += 4                               # substring, e.g. "rvlv"
            hit = ident = True
        if term in concept_text:
            score += 5
            hit = ident = True
        if term in alias_text:
            score += 2
            hit = ident = True
        if term in desc:
            score += 2
            hit = True
        matched += hit
        identifier_hits += ident

    all_terms = matched == len(terms)
    if not identifier_hits and not all_terms:
        return 0                                     # description noise only
    if all_terms:
        score += 8
    return score


def _search_columns_impl(query: str, table_name: str = "", limit: int = 25) -> str:
    """Find columns by meaning across the case's tables. See the block comment
    above for why this exists alongside `get_table_schema`."""
    _log_call("search_columns", {"query": query, "table_name": table_name,
                                 "limit": limit})
    if _catalog is None or _gateway is None or _gateway.get_case_id() is None:
        out = "Data unavailable: data layer is not initialized for this session."
        _log_result("search_columns", result=out)
        return out

    terms = _search_tokens(query)
    if not terms:
        out = (f"search_columns('{query}') — the query has no searchable terms. "
               f"Pass a concept or metric name, e.g. 'paydown rate'.")
        _log_result("search_columns", result=out, extra={"n_terms": 0})
        return out

    entries = _build_search_index(_gateway.get_case_id())
    if table_name:
        wanted = _resolve_real_table(table_name)
        entries = [e for e in entries if e["table"] in (wanted, table_name)]

    query_norm = _normalize(query)
    scored = [(s, e) for e in entries
              if (s := _score_entry(e, terms, query_norm)) > 0]
    # Highest score first; ties broken so catalog-documented columns lead, then
    # alphabetically for a stable, reproducible ordering.
    scored.sort(key=lambda se: (-se[0], not se[1]["in_catalog"],
                                se[1]["table"], se[1]["column"]))
    hits = scored[:limit]

    if not hits:
        # A benign negative, NOT a tool failure: the specialist should widen the
        # query or fall back to get_table_schema. Deliberately phrased to avoid
        # the failure markers that grounding.classify_tool_output looks for.
        out = (f"search_columns('{query}') — no columns matched. Try a broader "
               f"term, or call list_available_tables / get_table_schema to browse.")
        _log_result("search_columns", result=out, extra={"n_hits": 0})
        return out

    by_table: dict[str, list] = {}
    for score, e in hits:
        by_table.setdefault(e["table"], []).append((score, e))

    lines = [
        f"search_columns('{query}') — {len(hits)} match"
        f"{'' if len(hits) == 1 else 'es'} across {len(by_table)} table"
        f"{'' if len(by_table) == 1 else 's'}, best first. "
        f"Column names are as they appear in THIS case's data — pass them verbatim."
    ]
    for table, rows in by_table.items():
        lines.append(f"\n{table}:")
        for _score, e in rows:
            bits = [f"  - {e['column']} ({e['dtype']})"]
            if e["concepts"]:
                bits.append(f"[concept: {', '.join(e['concepts'])}]")
            lines.append(" ".join(bits))
            detail = e["description"] or ("(not in catalog — name only)"
                                          if not e["in_catalog"] else "")
            if e["threshold"]:
                detail = f"{detail} ({e['threshold']})".strip()
            if detail:
                lines.append(f"      {detail}")
    out = "\n".join(lines)
    _log_result("search_columns", result=out,
                extra={"n_hits": len(hits), "n_terms": len(terms),
                       "top": [e["column"] for _s, e in hits[:5]]})
    return out


@function_tool
def search_columns(query: str, table_name: str = "", limit: int = 25) -> str:
    """Find data columns by MEANING when you don't know the exact column name.

    Searches every column present in this case — by name, alias, catalog
    concept, and description — and returns the best matches with their table.
    Use this BEFORE assuming a metric is unavailable: the column that answers a
    question is often one your skill never names (e.g. "internal paydown rate"
    → `last_cycle_cut_revolve_rate`). Then confirm with `get_table_schema` and
    query it.

    Args:
        query: The metric or concept in the user's words, e.g. "paydown rate",
            "revolve utilization", "days past due".
        table_name: Optional — restrict the search to one table.
        limit: Max matches to return (default 25).
    """
    return _search_columns_impl(query, table_name, limit)


def _query_table_impl(
    table_name: str,
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    columns: str = "",
    filters: str = "",
    sort_by: str = "",
    sort_desc: bool = False,
    limit: int = 0,
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
        filter_op: one of "eq" (default), "ne", "gt", "gte", "lt", "lte",
            "between", "contains" (case-insensitive substring match), "in"
            (match any of a comma-separated value list — the multi-key eq).
            Use range ops for time windows — e.g. for payments in the 3 months
            before cut-off 2025-12-01, call:
                query_table("payments", filter_column="payment_date",
                            filter_op="gte", filter_value="2025-09-01")
            Or use "between" to bound both sides. Use "contains" for free-text
            entity columns (merchant name, reason codes) — e.g.
                query_table("spends", filter_column="merchant",
                            filter_op="contains", filter_value="starbucks")
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

    # Deterministic within a turn: an exact repeat returns the exact same rows.
    # Short-circuit it with a directive to CHANGE the query rather than re-dump.
    _sig = (table_name, filter_column, filter_value, filter_op, columns,
            filters, sort_by, bool(sort_desc), limit)
    _repeat = _seen_this_turn("query_table", _sig)
    if _repeat >= 1:
        out = json.dumps({
            "table": table_name,
            "repeated_call": True,
            "message": (
                f"You already ran this IDENTICAL query_table call {_repeat + 1} "
                "times this turn. query_table is deterministic — re-issuing it "
                "returns the SAME rows; it does not page or reveal new rows. To "
                "get DIFFERENT rows, CHANGE the query: add `filters` (a JSON "
                "AND-list of {column,value,op}), or `sort_by`+`sort_desc`+`limit` "
                "for the true top-N, or use `summarize_by_group` / "
                "`summarize_trend` to characterize the set. If you already have "
                "what you need, STOP querying and emit SpecialistOutput."
            ),
        }, indent=2)
        _log_result("query_table", result=out,
                    extra={"reason": "duplicate_call", "repeat": _repeat + 1,
                           "table_name": table_name})
        return out

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

    # Build the AND'd condition list. The single filter_column/value/op is the
    # simple case; `filters` (a JSON list of {"column","value","op"}) adds
    # compound conditions so ONE call can express e.g. a date window AND a
    # threshold — a single filter_column cannot, which used to force
    # specialists into many serial single-filter probes (and MaxTurns fails).
    conditions: list[tuple[str, str, str]] = []
    if filter_column and filter_value != "":
        conditions.append((filter_column, str(filter_value), filter_op or "eq"))
    if filters:
        try:
            parsed_filters = json.loads(filters)
        except (ValueError, TypeError):
            parsed_filters = None
        if isinstance(parsed_filters, list):
            for f in parsed_filters:
                if not isinstance(f, dict):
                    continue
                fc = f.get("column", f.get("filter_column"))
                fv = f.get("value", f.get("filter_value"))
                fo = f.get("op", f.get("filter_op", "eq"))
                if fc is None or fv is None:
                    continue
                conditions.append((str(fc), str(fv), str(fo or "eq")))

    # Resolve case/space variants ('return_flag' → 'Return Flag') against the
    # real CSV headers before filtering. Without this, a specialist following
    # the skill's snake_case names silently gets 0 rows.
    filter_parts: list[str] = []
    # Keep the pre-filter rows so a zero-match result can say WHY (see
    # `_zero_match_diagnostic`). Cheap: the gateway already returned this list.
    unfiltered_rows = rows
    resolved_conditions: list[tuple[str, str, str]] = []
    for fc, fv, fo in conditions:
        rc = _resolve_real_column(rows, fc, table_name)
        resolved_conditions.append((rc, str(fv), str(fo)))
        rows = _apply_filter(rows, rc, fv, fo)
        filter_parts.append(
            f"{rc} {fo} {fv!r} (resolved from '{fc}')" if rc != fc
            else f"{fc} {fo} {fv!r}"
        )
    filter_descriptor: str | None = " AND ".join(filter_parts) if filter_parts else None
    rows_matching_filter = len(rows)

    # Sort + top-N limit (optional) — lets "top 20 by tsr in May-2025" resolve
    # in ONE call instead of dumping 1000+ truncated rows the model can't rank.
    # `rows_matching_filter` above is the TRUE match count (before the limit);
    # the limit only bounds which rows are returned.
    sort_descriptor: str | None = None
    if sort_by and rows:
        rc_sort = _resolve_real_column(rows, sort_by, table_name)

        def _sort_key(r):
            v = r.get(rc_sort)
            try:
                return (0, float(v))
            except (TypeError, ValueError):
                return (1, str(v))

        rows = sorted(rows, key=_sort_key, reverse=bool(sort_desc))
        sort_descriptor = f"{rc_sort} {'desc' if sort_desc else 'asc'}"
    limit_applied: int | None = None
    if limit and limit > 0 and len(rows) > limit:
        rows = rows[:limit]
        limit_applied = limit

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

        # Step 2: reduce rows until JSON fits. For an UNSORTED, unlimited result
        # the first N rows are arbitrary and cluster on one date/value (which the
        # model mistakes for the whole match set) — so sample EVENLY across the
        # full set instead of taking the head. When the caller sorted or limited,
        # the head IS the intended top-N, so keep it in order.
        _even = not sort_descriptor and limit_applied is None
        _full_rows = list(rows)
        text = json.dumps(rows, indent=2, default=str)
        while len(text) > _MAX_CHARS - 500 and len(rows) > 1:
            target = max(1, len(rows) // 2)
            sampled = _even_sample(_full_rows, target) if _even else rows[:target]
            # GUARANTEE PROGRESS. `_even_sample` returns its input UNCHANGED
            # when max_items < 2, so once halving reached target=1 this loop
            # got all rows back, re-measured the full size, and spun forever —
            # an unbounded hang on any table wide enough that 2 rows exceed the
            # budget with no sort_by/limit (e.g. query_table("model_scores"),
            # 44 columns). A plain head-slice always shrinks.
            if len(sampled) >= len(rows):
                sampled = rows[:target]
            rows = sampled
            text = json.dumps(rows, indent=2, default=str)
        if limit_applied is not None:
            note = (f"top {min(limit_applied, rows_matching_filter)} of "
                    f"{rows_matching_filter} matching rows")
            if sort_descriptor:
                note += f" sorted by {sort_descriptor}"
            if len(rows) < limit_applied:
                note += (f" — only {len(rows)} shown due to size, "
                         "narrow with `columns`")
            truncation_notes.append(note)
        elif len(rows) < rows_matching_filter:
            truncation_notes.append(
                f"showing {len(rows)}/{rows_matching_filter} rows"
                + (" (an EVENLY-SPACED sample across the whole match set, NOT the "
                   "full set — use summarize_by_group / sort_by+limit to "
                   "characterize)" if _even else ""))

    rows_returned = len(rows)
    truncated = bool(truncation_notes)

    # Zero matches: say WHY rather than leaving the specialist to guess between
    # a wrong column, a wrong value vocabulary, and an honest absence.
    zero_diag = (_zero_match_diagnostic(unfiltered_rows, resolved_conditions)
                 if rows_matching_filter == 0 and resolved_conditions else None)

    response: dict[str, Any] = {
        "table": table_name,
        "filter": filter_descriptor,
        "sort": sort_descriptor,
        "limit": limit_applied,
        "columns_requested": requested_cols,
        "total_rows_in_table": total_rows_in_table,
        "rows_matching_filter": rows_matching_filter,
        **({"zero_match_diagnostic": zero_diag} if zero_diag else {}),
        "rows_returned": rows_returned,
        "truncated": truncated,
        "rows": rows,
    }
    if truncated:
        response["truncation_note"] = ", ".join(truncation_notes)
        advice = (
            "rows_matching_filter is the TRUE count; the rows array is a display "
            "sample — do NOT count its entries, and do NOT describe the full "
            "match set from it."
        )
        if not sort_descriptor and limit_applied is None:
            # Describe the sampling ACTUALLY used. This used to say "the FIRST
            # rows in TABLE ORDER" unconditionally, contradicting the
            # truncation_note in the same payload once even-sampling landed —
            # the model was handed two different stories about the same rows.
            how = ("spread evenly across the whole match set" if _even
                   else "the FIRST rows in TABLE ORDER")
            advice += (
                f" These sample rows are {how}, and are NOT a summary of the "
                f"matches. To characterize them, use `summarize_by_group` for "
                f"the distribution, or `sort_by`+`limit` for the true top-N — "
                f"never report this raw sample as the answer."
            )
        response["count_advice"] = advice

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
    filters: str = "",
    sort_by: str = "",
    sort_desc: bool = False,
    limit: int = 0,
) -> str:
    """Query a data table for the current case. All data is scoped to the active case.

    Returns a JSON object: {table, filter, sort, limit, total_rows_in_table,
    rows_matching_filter, rows_returned, truncated, rows: [...]}.

    For 'how many' / count questions: ALWAYS read `rows_matching_filter` from
    the response. The `rows` array is a display sample that may be truncated
    when the table is large — counting its entries gives the wrong answer.

    EXTRACTION questions ("show the abnormal transactions where X is high around
    May 2025") — resolve them in ONE call: combine conditions with `filters`,
    then `sort_by` + `limit` for the top-N. Do NOT issue many serial single-
    filter queries on a truncated dump — that thrashes and blows the round cap.

    Args:
        table_name: the table to query.
        filter_column: single column to filter on (optional; the simple case).
        filter_value: value(s) for the filter. For ``filter_op="between"`` pass
            "<low>,<high>" (inclusive). For ISO dates (YYYY-MM-DD) and YYYY-MM
            strings, lexicographic order matches chronological order.
        filter_op: one of "eq" (default), "ne", "gt", "gte", "lt", "lte",
            "between", "contains" (case-insensitive substring match), "in"
            (match any of a comma-separated value list — the multi-key eq).
        columns: comma-separated list of column names to return (e.g.
            "fico_score,derog_count"). Leave empty to return all columns.
            Pair with `limit` to keep the top-N rows within the size budget.
        filters: JSON list of compound AND conditions, each
            ``{"column": ..., "value": ..., "op": ...}`` (op defaults to "eq").
            ANDed together with filter_column. Use this to express e.g. a date
            window AND a threshold in a single call — e.g.
            ``'[{"column":"trans_dt","op":"between","value":"2025-05-01,2025-05-31"},
               {"column":"tot_struct_risk_score","op":"gt","value":"20"}]'``.
        sort_by: column to sort the matching rows by (optional).
        sort_desc: True for descending (largest first) — use with `sort_by` +
            `limit` to pull the top-N (e.g. highest-risk transactions).
        limit: return only the first N rows after sorting (top-N). 0 = no limit.
            `rows_matching_filter` still reports the TRUE match count.
    """
    return _query_table_impl(
        table_name=table_name,
        filter_column=filter_column,
        filter_value=filter_value,
        filter_op=filter_op,
        columns=columns,
        filters=filters,
        sort_by=sort_by,
        sort_desc=sort_desc,
        limit=limit,
    )


def _batch_query_table_impl(specs_json: str) -> str:
    """Run multiple ``query_table`` queries in one tool call. See
    ``batch_query_table`` for the contract."""
    _log_call("batch_query_table", {
        "specs_json": specs_json[:1000] if isinstance(specs_json, str) else str(specs_json)[:1000]})
    specs = _salvage_specs_list(specs_json)
    if not specs:
        out = _unparseable_specs_directive(
            "batch_query_table", specs_json,
            '[{"table_name":"spends","columns":"Date,Amount,Merchant Name","limit":20}]')
        _log_result("batch_query_table", result=out, extra={"reason": "specs_unparseable"})
        return out

    results: list[dict[str, Any]] = []
    for idx, spec in enumerate(specs[:6]):
        if not isinstance(spec, dict):
            results.append({"index": idx, "error": "spec must be an object"})
            continue
        raw_filters = spec.get("filters")
        filters_arg = (
            json.dumps(raw_filters)
            if isinstance(raw_filters, (list, dict))
            else str(raw_filters or "")
        )
        result = _query_table_impl(
            table_name=str(spec.get("table_name") or spec.get("table") or ""),
            filter_column=str(spec.get("filter_column") or ""),
            filter_value=str(spec.get("filter_value") or ""),
            filter_op=str(spec.get("filter_op") or "eq"),
            columns=str(spec.get("columns") or ""),
            filters=filters_arg,
            sort_by=str(spec.get("sort_by") or ""),
            sort_desc=bool(spec.get("sort_desc") or False),
            limit=int(spec.get("limit") or 0),
        )
        results.append({"index": idx, "result": result})

    payload: dict[str, Any] = {"results": results}
    if len(specs) > 6:
        payload["truncated"] = f"ran first 6 of {len(specs)} requested queries"
    out = json.dumps(payload, indent=2, default=str)
    _log_result("batch_query_table", result=out,
                extra={"n_requested": len(specs), "n_run": len(results)})
    return out


@function_tool
def batch_query_table(specs_json: str) -> str:
    """Run multiple INDEPENDENT ``query_table`` queries in one tool call.

    Use when a question needs several UNRELATED row-extractions from the same
    case (e.g. pull the abnormal transactions from two different tables, or the
    same table under two disjoint windows). Batching them saves one LLM round
    per query and keeps the specialist from blowing its per-turn round budget
    with serial calls.

    ``specs_json`` is a JSON list; each item accepts the same arguments as
    ``query_table`` (including ``filters``, ``sort_by``, ``sort_desc``,
    ``limit``). Example:

    ``[{"table_name": "modelling_data_transaction",
        "filters": [{"column":"trans_dt","op":"between","value":"2025-05-01,2025-05-31"},
                    {"column":"tot_struct_risk_score","op":"gt","value":"20"}],
        "sort_by": "tot_struct_risk_score", "sort_desc": true, "limit": 10,
        "columns": "trans_dt,tot_struct_risk_score"}]``

    NOTE: for ONE query that needs several AND'd conditions, use ``query_table``
    with ``filters`` directly — batch is only for MULTIPLE separate queries.
    Runs at most 6.
    """
    return _batch_query_table_impl(specs_json)


def _join_table_impl(
    left_table: str,
    right_table: str,
    left_on: str,
    right_on: str = "",
    columns: str = "",
    how: str = "inner",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    """Join two tables on a key column for the current case. See ``join_table``."""
    _log_call("join_table", {
        "left_table": left_table, "right_table": right_table,
        "left_on": left_on, "right_on": right_on, "how": how,
        "filter": (f"{filter_column} {filter_op} {filter_value}"
                   if filter_column else None),
    })
    if _gateway is None:
        out = ("Data unavailable: data layer is not initialized for this session "
               "(no gateway bound). This is an infrastructure error, not a finding.")
        _log_result("join_table", result=out, extra={"reason": "no_gateway_bound"})
        return out

    right_on = right_on or left_on
    how = (how or "inner").lower()
    lt = _resolve_real_table(left_table)
    rt = _resolve_real_table(right_table)
    lrows = _gateway.query(lt, filters=None)
    rrows = _gateway.query(rt, filters=None)
    if lrows is None or rrows is None:
        missing = left_table if lrows is None else right_table
        out = f"Data unavailable: table '{missing}' not found for current case."
        _log_result("join_table", result=out, extra={"missing": missing})
        return out

    # Narrow the LEFT table BEFORE joining (keeps the join small + fast). The
    # common pattern: filter left to the transactions of interest, then join
    # the right table's scores/drivers onto them.
    if filter_column and filter_value != "":
        lcol = _resolve_real_column(lrows, filter_column, lt)
        lrows = _apply_filter(lrows, lcol, str(filter_value), filter_op)
    left_n = len(lrows)

    l_on = _resolve_real_column(lrows, left_on, lt) if lrows else left_on
    r_on = _resolve_real_column(rrows, right_on, rt) if rrows else right_on

    # Index the right table by join key (normalized so ms-vs-second timestamps,
    # numeric strings, and case/space text variants all match).
    index: dict = {}
    for rr in rrows:
        k = _join_key(rr.get(r_on))
        if k is not None:
            index.setdefault(k, []).append(rr)

    merged: list[dict] = []
    for lr in lrows:
        k = _join_key(lr.get(l_on))
        matches = index.get(k, []) if k is not None else []
        if not matches:
            if how == "left":
                merged.append(dict(lr))
            continue
        for rr in matches:
            row = dict(lr)
            for col, val in rr.items():
                if col == r_on and r_on == l_on:
                    continue  # don't duplicate the shared join column
                # Prefix a colliding right column so no data is lost.
                row[col if col not in lr else f"{right_table}.{col}"] = val
            merged.append(row)
    matched_n = len(merged)

    requested_cols: list[str] | None = None
    if columns and merged:
        requested = [c.strip() for c in columns.split(",") if c.strip()]

        def _pick(row: dict, name: str) -> str | None:
            for cand in (name, f"{right_table}.{name}"):
                if cand in row:
                    return cand
            return None

        if requested:
            requested_cols = requested
            merged = [
                {name: row[_pick(row, name)]
                 for name in requested if _pick(row, name) is not None}
                for row in merged
            ]

    truncation_notes: list[str] = []
    if merged:
        text = json.dumps(merged, indent=2, default=str)
        while len(text) > _MAX_CHARS - 500 and len(merged) > 1:
            merged = merged[: len(merged) // 2]
            text = json.dumps(merged, indent=2, default=str)
        if len(merged) < matched_n:
            truncation_notes.append(f"showing {len(merged)}/{matched_n} joined rows")

    response: dict[str, Any] = {
        "left_table": lt,
        "right_table": rt,
        "join": f"{l_on} = {r_on} ({how})",
        "left_rows_after_filter": left_n,
        "matched_rows": matched_n,
        "rows_returned": len(merged),
        "columns_requested": requested_cols,
        "truncated": bool(truncation_notes),
        "rows": merged,
    }
    # FAN-OUT. When the right key is not unique, one left row matches several
    # right rows and `matched_rows` exceeds the left side — 9,021 joined rows
    # from 8,888 spends, because 64 timestamps repeat in the transaction table
    # (worst case 5 rows on one instant). Unflagged, `matched_rows` reads as a
    # transaction count and every downstream total is inflated. This is the
    # same failure family as the top-N share error: a number that looks like
    # an answer but is measured over the wrong set.
    if matched_n > left_n:
        response["fan_out"] = {
            "detected": True,
            "note": (
                f"{matched_n:,} joined rows from {left_n:,} left rows — the "
                f"right key `{r_on}` is NOT unique, so one left row matched "
                f"several right rows. Joined rows are NOT distinct "
                f"{lt} records: do NOT count them as such, and do not SUM a "
                f"left-side column over them (it double-counts). Aggregate on "
                f"the left table directly, or de-duplicate on `{l_on}` first."
            ),
        }
    if truncation_notes:
        response["truncation_note"] = ", ".join(truncation_notes)
        response["count_advice"] = (
            "matched_rows is the true JOINED-ROW count (see fan_out if present, "
            "it may exceed the number of distinct left records); the rows array "
            "is a display sample and may be truncated — do NOT count its "
            "entries, and do not characterize the full set from it. Use "
            "`summarize_by_group` or `aggregate_column` on the source table for "
            "distributions and totals."
        )
    out = json.dumps(response, indent=2, default=str)
    _log_result("join_table", result=out,
                extra={"left_table": lt, "right_table": rt,
                       "left_after_filter": left_n, "matched": matched_n})
    return out


@function_tool
def join_table(
    left_table: str,
    right_table: str,
    left_on: str,
    right_on: str = "",
    columns: str = "",
    how: str = "inner",
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
) -> str:
    """Join two tables on a key column, for the current case — one call instead
    of a manual "query A, then look each key up in B" loop.

    Canonical use: attach per-transaction model scores / drivers to a set of
    spend transactions. Filter the LEFT table to the transactions you care
    about (via filter_column/value/op), then join the RIGHT table onto them:

    ``join_table("spends", "model_scores_transaction",
                 left_on="Timestamp", right_on="txn_date_time",
                 filter_column="Merchant Name", filter_op="contains",
                 filter_value="S BERTRAM",
                 columns="Date,Merchant Name,Amount,tot_struct_risk_score,credit_loss_prob")``

    Key matching is tolerant: a spend's millisecond `Timestamp` matches the
    model table's second-precision `txn_date_time`; numeric and case/space text
    variants also match.

    Args:
        left_table / right_table: tables to join.
        left_on / right_on: join key columns (right_on defaults to left_on).
        columns: comma-separated output columns from EITHER side (empty = all).
            A right column whose name collides with a left one is prefixed
            ``<right_table>.<col>``.
        how: "inner" (default — only matched rows) or "left" (keep unmatched
            left rows, right columns absent).
        filter_column / filter_value / filter_op: optional filter applied to the
            LEFT table BEFORE the join — always narrow first on high-volume
            tables. Returns {left_table, right_table, join, left_rows_after_filter,
            matched_rows, rows_returned, rows: [...]}.
    """
    return _join_table_impl(
        left_table=left_table, right_table=right_table,
        left_on=left_on, right_on=right_on, columns=columns, how=how,
        filter_column=filter_column, filter_value=filter_value,
        filter_op=filter_op,
    )


# Canonical schema for the per-transaction "detail" join: spend identity +
# model scores + top drivers, all keyed on the transaction timestamp. Column
# names are resolved leniently and any that are absent for a case are simply
# omitted, so a case missing (say) the drivers table still returns scores.
_TXN_DETAIL_CFG = {
    "score_table": "model_scores_transaction",
    "driver_table": "score_drivers_transaction",
    "spend_key": "Timestamp",
    "txn_key": "txn_date_time",
    # A full per-transaction record: time + merchant + amount + spend variables
    # (from spends) + risk scores (from model_scores_transaction) + top score
    # drivers (from score_drivers_transaction). Columns absent for a case are
    # skipped; pass `columns` to override the set entirely.
    "spend_cols": ["Timestamp", "Merchant Name", "Merchant Industry", "Amount",
                   "Spend Concentration", "RNN Spend Score",
                   "Spend Divergence Index"],
    "score_cols": ["tot_struct_risk_score", "credit_loss_prob"],
    # Drivers are per-score AND directional: `top_*` push the score UP (raise
    # risk), `bottom_*` push it DOWN (mitigate). CDSS and TSR have DIFFERENT
    # drivers, so keep them separate.
    "driver_cols": ["top_cdss1", "top_cdss2", "bottom_cdss1",
                    "top_tsr1", "top_tsr2", "bottom_tsr1"],
}


# A driver column holds the NAME of a feature, not a value: `top_cdss1` =
# "last_cycle_cut_revolve_rate". Matches top_/bottom_ + family + rank.
_DRIVER_COL_RE = re.compile(r"^(top|bottom)_[a-z]+\d+$", re.IGNORECASE)


# The real monthly modeling export ships per-month AGGREGATES of each model
# feature, suffixed by the aggregation: `cbr_score` → `cbr_score_max`. The
# driver tables reference the UNSUFFIXED feature name, and only some columns
# declare the suffixed form as a catalog alias (31 `_max` / 9 `_min` / 1 `_mean`
# in the shipped real case, most undeclared). So after the normal alias +
# normalization resolution fails, try the suffixes — otherwise the majority of
# drivers come back name-only, which is the state this feature exists to fix.
_DRIVER_VALUE_SUFFIXES = ("_max", "_min", "_mean")


def _resolve_driver_feature(row: dict, feature: str, table: str,
                            cache: dict) -> str | None:
    """Real column in `row` holding `feature`'s value, or None if absent here.

    The cache saves re-running a (potentially ~250-column) resolution per
    distinct driver, but it stores only the resolved NAME — membership is
    re-checked against the row every time. Rows do NOT share a key set:
    `transaction_detail` merges a LEFT join, so a transaction with no
    `model_scores_transaction` match simply lacks those columns. Trusting the
    cached name blind raised `KeyError: 'cust_intr_extnl_unscr_tt_debt_srvc_rt1'`
    on the first such row.
    """
    if feature in cache:
        real = cache[feature]
        return real if (real is not None and real in row) else None
    real = _resolve_real_column([row], feature, table)
    if real not in row:
        real = next((c for s in _DRIVER_VALUE_SUFFIXES
                     if (c := f"{feature}{s}") in row), None)
    cache[feature] = real if (real and real in row) else None
    return cache[feature]


def _attach_driver_values(merged: dict, driver_names: list[str],
                          score_table: str, resolve_cache: dict) -> dict:
    """Map each driver FEATURE NAME in this row to its value on the same row.

    A bare driver name tells a case reviewer which feature moved the score but
    not by how much — and the value is already sitting in the joined
    `model_scores_transaction` row, so answering "how bad is it" should not cost
    another query. Real-data profiles suffix these features (`_min` / `_max`),
    so names resolve through the catalog's aliases rather than by exact key.

    Returned as ONE deduplicated `{feature: value}` map per row instead of a
    sibling key per driver column: CDSS and TSR routinely cite the same feature,
    and `transaction_detail` truncates on total characters, so the dedup buys
    back rows.
    """
    values: dict = {}
    for feature in driver_names:
        real = _resolve_driver_feature(merged, feature, score_table, resolve_cache)
        if real is None:
            continue
        val = merged.get(real)      # .get, not [] — see the resolver's note
        if val not in ("", None):
            values[feature] = val
    return values


# Timestamp join column per transaction table (all three share the same
# instant at different precision — matched via `_join_key` at second grain).
_TXN_TABLE_KEYS = {
    "spends": "Timestamp",
    "model_scores_transaction": "txn_date_time",
    "score_drivers_transaction": "txn_date_time",
}


def _transaction_detail_impl(
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    filters: str = "",
    timestamps: str = "",
    sort_by: str = "",
    sort_desc: bool = False,
    limit: int = 0,
    columns: str = "",
    base_table: str = "spends",
) -> str:
    """One-call denormalized per-transaction record. See ``transaction_detail``."""
    _log_call("transaction_detail", {
        "base_table": base_table,
        "filter": (f"{filter_column} {filter_op} {filter_value}"
                   if filter_column else None),
        "filters": filters[:300] if filters else None,
        "timestamps": (timestamps[:120] if timestamps else None),
        "sort_by": sort_by, "limit": limit,
    })
    if _gateway is None:
        out = ("Data unavailable: data layer is not initialized for this session "
               "(no gateway bound). Infrastructure error, not a finding.")
        _log_result("transaction_detail", result=out, extra={"reason": "no_gateway"})
        return out

    cfg = _TXN_DETAIL_CFG
    base_table = base_table or "spends"
    base_key_name = _TXN_TABLE_KEYS.get(base_table, "Timestamp")
    base_t = _resolve_real_table(base_table)
    brows = _gateway.query(base_t, filters=None)
    if brows is None:
        out = f"Data unavailable: base table '{base_table}' not found for current case."
        _log_result("transaction_detail", result=out, extra={"reason": "no_base"})
        return out

    # ── select the transactions of interest FROM the base table ──
    if timestamps:
        key = _resolve_real_column(brows, base_key_name, base_t)
        brows = _apply_filter(brows, key, timestamps, "in")
    else:
        conds: list[tuple[str, str, str]] = []
        if filter_column and filter_value != "":
            conds.append((filter_column, str(filter_value), filter_op or "eq"))
        if filters:
            try:
                parsed = json.loads(filters)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                for f in parsed:
                    if not isinstance(f, dict):
                        continue
                    fc = f.get("column", f.get("filter_column"))
                    fv = f.get("value", f.get("filter_value"))
                    fo = f.get("op", f.get("filter_op", "eq"))
                    if fc is not None and fv is not None:
                        conds.append((str(fc), str(fv), str(fo or "eq")))
        for fc, fv, fo in conds:
            rc = _resolve_real_column(brows, fc, base_t)
            brows = _apply_filter(brows, rc, fv, fo)
    n_txn = len(brows)

    if sort_by and brows:
        rc_sort = _resolve_real_column(brows, sort_by, base_t)

        def _sort_key(r):
            v = r.get(rc_sort)
            try:
                return (0, float(v))
            except (TypeError, ValueError):
                return (1, str(v))

        brows = sorted(brows, key=_sort_key, reverse=bool(sort_desc))
    if limit and limit > 0 and len(brows) > limit:
        brows = brows[:limit]

    # ── index the OTHER two transaction tables by their timestamp column ──
    def _index(table_name: str, key_name: str) -> dict:
        rows = _gateway.query(_resolve_real_table(table_name), filters=None)
        idx: dict = {}
        if rows:
            rc = _resolve_real_column(rows, key_name, table_name)
            for rr in rows:
                k = _join_key(rr.get(rc))
                if k is not None:
                    idx.setdefault(k, rr)  # first row wins on a rare collision
        return idx

    join_tables = [t for t in ("spends", cfg["score_table"], cfg["driver_table"])
                   if t != base_table]
    idxs = {t: _index(t, _TXN_TABLE_KEYS.get(t, "txn_date_time")) for t in join_tables}
    base_key = (_resolve_real_column(brows, base_key_name, base_t)
                if brows else base_key_name)

    want = ([c.strip() for c in columns.split(",") if c.strip()] if columns
            else cfg["spend_cols"] + cfg["score_cols"] + cfg["driver_cols"])
    # Always surface a timestamp: for a model/driver base the row's own
    # `txn_date_time` is the time (Timestamp only appears when a spend joins).
    if not columns and base_key_name not in want:
        want = [base_key_name] + want

    # ── merge one record per transaction (LEFT join from the base table) ──
    driver_cols_wanted = [c for c in want if _DRIVER_COL_RE.match(c)]
    resolve_cache: dict = {}
    out_rows: list[dict] = []
    match_counts = {t: 0 for t in join_tables}
    for br in brows:
        k = _join_key(br.get(base_key))
        merged = dict(br)
        for t in join_tables:
            m = idxs[t].get(k) if k is not None else None
            if m:
                merged.update(m)
                match_counts[t] += 1
        row = {name: merged[name] for name in want if name in merged}
        # Attach the VALUE behind each driver name. Driver columns keep their
        # bare feature name (callers match on it); the values ride alongside.
        if driver_cols_wanted:
            names = list(dict.fromkeys(
                v for c in driver_cols_wanted
                if isinstance(v := merged.get(c), str) and v.strip()))
            if names:
                dv = _attach_driver_values(
                    merged, names, cfg["score_table"], resolve_cache)
                if dv:
                    row["driver_values"] = dv
        out_rows.append(row)

    matched_n = len(out_rows)
    # Coverage of the merchant/amount side across the FULL selected set (before
    # any size truncation) — so "N of M rows carry a settled spend" is reported
    # from the real join, and merchant-less rows read as auths/declines that
    # never settled, NOT as a broken join.
    _spend_tbl = cfg.get("spend_table", "spends")
    n_with_merchant = match_counts.get(_spend_tbl, 0) if base_table != _spend_tbl else matched_n
    truncation_notes: list[str] = []
    if out_rows:
        text = json.dumps(out_rows, indent=2, default=str)
        # Uniformly sub-sample (keep first & last, spread the middle) rather than
        # head-halving: head-halving keeps the FIRST rows, so a TSR-desc sort left
        # only the extreme model-only rows and dropped every settled spend.
        if len(text) > _TXN_DETAIL_MAX_CHARS:
            target = len(out_rows)
            while len(text) > _TXN_DETAIL_MAX_CHARS and target > 1:
                target = max(1, int(target * 0.75))
                out_rows = _even_sample(out_rows, target)
                text = json.dumps(out_rows, indent=2, default=str)
        if len(out_rows) < matched_n:
            truncation_notes.append(
                f"showing {len(out_rows)}/{matched_n} transactions "
                "(uniformly sampled across the selection)"
            )

    # `with_model_scores` retained for back-compat; joined_match_counts gives the
    # full picture so a partial join reads as "N of M had a settled spend", not
    # a failure.
    with_scores = (n_txn if base_table == cfg["score_table"]
                   else match_counts.get(cfg["score_table"], 0))
    response: dict[str, Any] = {
        "base_table": base_t,
        "transactions_selected": n_txn,
        # The join counts are measured over the rows this call actually
        # MERGED, which `limit` truncates — they are NOT out of
        # `transactions_selected`. Stating the denominator explicitly matters:
        # `limit=3` reported `with_model_scores: 3` beside
        # `transactions_selected: 8888`, which reads as "only 3 of 8,888
        # transactions are model-scored" when the true figure is 8,866.
        "transactions_examined": matched_n,
        "with_model_scores": with_scores,
        "joined_match_counts": {t: match_counts[t] for t in join_tables},
        "joined_match_counts_note": (
            f"counts are out of transactions_examined ({matched_n:,})"
            + (f", NOT the {n_txn:,} selected — `limit` truncated the merge, so "
               f"these coverage figures describe the examined subset only"
               if matched_n < n_txn else "")
        ),
        "merchant_amount_coverage": (
            f"{n_with_merchant} of {matched_n} examined transactions have a "
            f"settled spend (so carry Merchant Name + Amount); the other "
            f"{matched_n - n_with_merchant} are model-scored auths/declines that "
            f"never settled — merchant/amount are legitimately absent for those, "
            f"NOT a join failure. Report the merchant/amount you DO have."),
        "rows_returned": len(out_rows),
        "schema_note": (
            "one row per transaction, joined on the transaction timestamp: time + "
            "merchant + amount + spend variables + risk scores (TSR = "
            "tot_struct_risk_score, CDSS = credit_loss_prob, the customer score) + "
            "score drivers — `top_*` raise the score, `bottom_*` lower it; CDSS "
            "and TSR have DIFFERENT drivers (top_/bottom_cdss* vs top_/bottom_tsr*). "
            "`driver_values` gives the VALUE of each driver feature on that same "
            "transaction (from the modeling table) — quote the driver WITH its "
            "value, e.g. 'last_cycle_cut_revolve_rate = 0.31'; a driver name "
            "alone does not tell the reviewer how far out of line it is. "
            "A transaction in the base table but "
            "absent from a joined table (e.g. a model-scored auth with no settled "
            "spend) keeps the columns it HAS — the missing side is simply absent, "
            "NOT a failure; see merchant_amount_coverage / joined_match_counts."),
        "truncated": bool(truncation_notes),
        "rows": out_rows,
    }
    if truncation_notes:
        response["truncation_note"] = ", ".join(truncation_notes)
    out = json.dumps(response, indent=2, default=str)
    _log_result("transaction_detail", result=out,
                extra={"base_table": base_t, "transactions": n_txn,
                       "coverage": {t: match_counts[t] for t in join_tables}})
    return out


@function_tool
def transaction_detail(
    filter_column: str = "",
    filter_value: str = "",
    filter_op: str = "eq",
    filters: str = "",
    timestamps: str = "",
    sort_by: str = "",
    sort_desc: bool = False,
    limit: int = 0,
    columns: str = "",
    base_table: str = "spends",
) -> str:
    """ONE-CALL denormalized per-transaction record: time + merchant + amount +
    spend variables + risk scores + top & bottom score drivers, pre-joined on the transaction
    timestamp across `spends` / `model_scores_transaction` /
    `score_drivers_transaction`. Replaces the manual 3-way join — just read a
    complete record and analyze it.

    Pick where the transactions are SELECTED FROM with `base_table`, then filter
    that table:
    - `base_table="spends"` (default) — select by a spend attribute
      (`filter_column="Merchant Name", filter_op="contains", filter_value="S BERTRAM"`,
      or a compound `filters` list), or by `timestamps` from a prior extraction.
    - `base_table="model_scores_transaction"` — select by a MODEL metric, e.g.
      "transactions where TSR reacted": `transaction_detail(base_table="model_scores_transaction",
      filter_column="tot_struct_risk_score", filter_op="gt", filter_value="20",
      sort_by="tot_struct_risk_score", sort_desc=true, limit=20)`. The score
      table covers more transactions than spends (auths/declines that never
      settle), so some rows will have NO merchant/amount — that is expected, not
      a failure; drivers still attach. `joined_match_counts` reports coverage.

    `sort_by`/`sort_desc`/`limit` apply to the base table (top-N). Each row
    carries the columns it HAS (missing joins are simply absent). CDSS is the
    CUSTOMER score `credit_loss_prob`; the merchant CDSS
    `cust_eff_se_cdss_5_180_day_score` is excluded by default (add via `columns`).
    Returns {base_table, transactions_selected, with_model_scores,
    joined_match_counts, rows: [...]}.
    """
    return _transaction_detail_impl(
        filter_column=filter_column, filter_value=filter_value,
        filter_op=filter_op, filters=filters, timestamps=timestamps,
        sort_by=sort_by, sort_desc=sort_desc, limit=limit, columns=columns,
        base_table=base_table,
    )


# ── score_driver_values ────────────────────────────────────────────────────
#
# The MONTHLY counterpart to transaction_detail's `driver_values`. Real cases
# ship `score_drivers` (which features moved CDSS / TSR each month) and
# `model_scores` (what those features were worth), joined on `trans_month` —
# but the driver table stores a feature NAME as its cell value, so no generic
# join expresses "use this cell's contents as a column name in that table".
# Without this, a reviewer reads "top CDSS driver: last_cycle_cut_revolve_rate"
# and still has to go ask what it was, which is the whole question.

_DRIVER_FAMILY_RE = re.compile(r"^(top|bottom)_([a-z]+?)(\d+)$", re.IGNORECASE)


def _driver_columns(row_keys) -> list[tuple[str, str, str, int]]:
    """(column, direction, family, rank) for each driver column, rank-ordered."""
    found = []
    for key in row_keys:
        m = _DRIVER_FAMILY_RE.match(str(key))
        if m:
            found.append((key, m.group(1).lower(), m.group(2).lower(),
                          int(m.group(3))))
    found.sort(key=lambda t: (t[2], t[1], t[3]))
    return found


def _score_driver_values_impl(period: str = "", score: str = "",
                              limit: int = 0) -> str:
    """Monthly score drivers WITH the value of each driver feature."""
    _log_call("score_driver_values",
              {"period": period, "score": score, "limit": limit})
    if _gateway is None or _catalog is None:
        out = ("Data unavailable: data layer is not initialized for this session "
               "(no gateway bound). Infrastructure error, not a finding.")
        _log_result("score_driver_values", result=out)
        return out

    drv_t = _resolve_real_table("score_drivers")
    mdl_t = _resolve_real_table("model_scores")
    drows = _gateway.query(drv_t, filters=None)
    if not drows:
        out = (f"Data unavailable: table '{drv_t}' not found for current case.")
        _log_result("score_driver_values", result=out, extra={"found": False})
        return out
    mrows = _gateway.query(mdl_t, filters=None) or []

    drv_key = _resolve_real_column(drows, "trans_month", drv_t)
    mdl_key = _resolve_real_column(mrows, "trans_month", mdl_t) if mrows else ""
    # Index the modeling table by month. `_date_key` normalizes the format gap
    # (the real export writes `July'2023`), so the two tables join even when
    # their date spellings differ — see the date-format notes in CLAUDE.md.
    by_month: dict = {}
    for mr in mrows:
        k = _date_key(mr.get(mdl_key))
        if k is not None:
            by_month.setdefault(k, mr)

    want_family = (score or "").strip().lower().replace("_", "") or ""
    period_key = _date_key(period) if period else None
    if period and period_key is None:
        out = (f"score_driver_values: could not parse period '{period}'. Pass a "
               f"month as it appears in the data (check get_table_schema), "
               f"e.g. \"July'2023\" or 2023-07.")
        _log_result("score_driver_values", result=out, extra={"bad_period": period})
        return out

    driver_cols = _driver_columns(drows[0].keys())
    resolve_cache: dict = {}
    months: list[dict] = []
    n_missing = 0
    for dr in drows:
        mkey = _date_key(dr.get(drv_key))
        if period_key is not None and mkey != period_key:
            continue
        mrow = by_month.get(mkey) if mkey is not None else None
        entry: dict = {"trans_month": dr.get(drv_key), "drivers": {}}
        for col, direction, family, rank in driver_cols:
            if want_family and family != want_family:
                continue
            feature = dr.get(col)
            if not isinstance(feature, str) or not feature.strip():
                continue
            feature = feature.strip()
            item: dict = {"rank": rank, "feature": feature}
            if mrow is not None:
                real = _resolve_driver_feature(mrow, feature, mdl_t, resolve_cache)
                if real is not None and mrow[real] not in ("", None):
                    item["value"] = mrow[real]
                    if real != feature:
                        # Surface the aggregation so the reviewer knows the value
                        # is that month's max/min, not a point-in-time reading.
                        item["value_column"] = real
                else:
                    n_missing += 1
            entry["drivers"].setdefault(f"{direction}_{family}", []).append(item)
        if entry["drivers"]:
            months.append(entry)

    if not months:
        out = (f"score_driver_values: no driver rows"
               f"{f' for period {period}' if period else ''}"
               f"{f' and score {score}' if score else ''}. "
               f"Call get_table_schema('{drv_t}') to see the months available.")
        _log_result("score_driver_values", result=out, extra={"n_months": 0})
        return out

    if limit and limit > 0:
        months = months[-limit:]        # most recent months

    response = {
        "driver_table": drv_t,
        "value_table": mdl_t,
        "months_returned": len(months),
        "unresolved_driver_values": n_missing,
        "schema_note": (
            "Per month, the features that moved each score, WITH their value "
            "that month. `top_*` push the score UP, `bottom_*` push it DOWN; "
            "CDSS and TSR have different drivers. `rank` 1 = strongest. Quote a "
            "driver WITH its value — the name alone does not tell the reviewer "
            "how far out of line it was. A missing `value` means the feature is "
            f"not a column of '{mdl_t}' for this case (counted in "
            "unresolved_driver_values), NOT a failed lookup."),
        "months": months,
    }
    out = json.dumps(response, indent=2, default=str)
    if len(out) > _MAX_CHARS * 2:
        months = months[-max(1, len(months) // 2):]
        response["months"] = months
        response["months_returned"] = len(months)
        response["truncated"] = True
        out = json.dumps(response, indent=2, default=str)
    _log_result("score_driver_values", result=out,
                extra={"n_months": len(months), "unresolved": n_missing})
    return out


@function_tool
def score_driver_values(period: str = "", score: str = "", limit: int = 0) -> str:
    """Monthly score drivers WITH the value of each driver feature attached.

    `score_drivers` names the features that moved CDSS / TSR each month, but
    stores only the NAME; the value lives in the modeling table. This joins the
    two so you can say "last_cycle_cut_revolve_rate = 0.31" instead of just
    naming it. Use for "why did the score move" / "what drove the decline".

    Args:
        period: Optional month filter, e.g. "July'2023" or "2023-07". Any format
            the data uses is accepted.
        score: Optional score family — "cdss" or "tsr". Omit for both.
        limit: Optional — keep only the N most recent months.
    """
    return _score_driver_values_impl(period, score, limit)


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


_BATCH_AGG_EXAMPLE = (
    '[{"table_name":"spends","column":"Amount","op":"sum"},'
    '{"table_name":"payments","column":"Payment Amount","op":"sum"}]'
)


def _batch_aggregate_impl(specs_json: str) -> str:
    _log_call("batch_aggregate", {
        "specs_json": specs_json[:1000] if isinstance(specs_json, str) else str(specs_json)[:1000]})
    specs = _salvage_specs_list(specs_json)
    if not specs:
        out = _unparseable_specs_directive(
            "batch_aggregate", specs_json, _BATCH_AGG_EXAMPLE)
        _log_result("batch_aggregate", result=out, extra={"reason": "specs_unparseable"})
        return out

    results: list[dict[str, Any]] = []
    for idx, spec in enumerate(specs[:10]):
        if not isinstance(spec, dict):
            results.append({
                "index": idx,
                "error": "spec must be an object",
            })
            continue
        result = _aggregate_column_impl(
            table_name=str(spec.get("table_name") or spec.get("table") or ""),
            column=str(spec.get("column") or ""),
            op=str(spec.get("op") or "sum"),
            filter_column=str(spec.get("filter_column") or ""),
            filter_value=str(spec.get("filter_value") or ""),
            filter_op=str(spec.get("filter_op") or "eq"),
        )
        results.append({"index": idx, "result": result})

    payload: dict[str, Any] = {"results": results}
    if len(specs) > 10:
        payload["truncated"] = f"ran first 10 of {len(specs)} requested aggregates"
    out = json.dumps(payload, indent=2, default=str)
    _log_result("batch_aggregate", result=out,
                extra={"n_requested": len(specs), "n_run": len(results)})
    return out


@function_tool
def batch_aggregate(specs_json: str) -> str:
    """Run multiple ``aggregate_column`` operations in one tool call.

    Use this when a question needs several scalar checks from the same case,
    especially windowed count answers that require count + first date + last
    date. This avoids one LLM round-trip per scalar.

    ``specs_json`` must be a JSON list. Each item accepts the same arguments
    as ``aggregate_column``:

    ``{"table_name": "payments", "column": "payment_date", "op": "count",
       "filter_column": "Return Flag", "filter_value": "1"}``  # 1 = returned, 0 = successful

    Returns JSON:
    ``{"results": [{"index": 0, "result": "count(...) = ..."}, ...]}``

    Keep batches focused: 2-6 scalar aggregates. For time series use
    ``summarize_trend`` instead; for category rankings use
    ``summarize_by_group`` instead.
    """
    return _batch_aggregate_impl(specs_json)


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
        # Two very different causes, and conflating them sent specialists in
        # circles: the DATES didn't parse, vs the dates were fine and the VALUE
        # column is empty for this case. The old message blamed the time column
        # either way, so a specialist trending an all-blank column (real case:
        # `bureau_data.SBFE Score`, 26 rows all blank) kept "fixing" a date
        # column that was never wrong. An empty column is a DATA GAP — the tool
        # worked, this case simply has no such data — so say that, and say it
        # is not worth retrying.
        # A column that is not PRESENT is a different failure from one that is
        # present but empty, and conflating them is actively harmful: the DATA
        # GAP branch below tells the specialist "do NOT retry this column", so a
        # simple name miss (`intoop` for `INTOOP`) would make it abandon a
        # variable that exists. This is a mistake to correct, not a gap to report.
        if rows and real_value not in rows[0]:
            out = (
                f"trend({op}({value_column}) by {period} on {time_column})"
                f"{filter_descr} = (COLUMN NOT FOUND: '{value_column}' is not a "
                f"column of '{real_table}' for this case — nothing was measured. "
                f"Call search_columns('{value_column}') to find the right name "
                f"(ADL/CAS aliases resolve), or get_table_schema('{real_table}') "
                f"to list what IS here, then re-issue. Do NOT report this as a "
                f"data gap.)"
            )
            _log_result("summarize_trend", result=out,
                        extra={"reason": "column_not_found",
                               "value_column": value_column,
                               "resolved_to": real_value})
            return out

        if n_in_range and n_value_skipped >= n_in_range:
            out = (
                f"trend({op}({value_column}) by {period} on {time_column})"
                f"{filter_descr} = (DATA GAP: column '{value_column}' is EMPTY "
                f"for this case — {n_in_range:,} row(s) in range, every one "
                f"blank or non-numeric; the {time_column} values parsed fine. "
                f"The tool worked; this case has no {value_column} data. Record "
                f"it in `data_gaps` and do NOT retry this column.)"
            )
            _log_result("summarize_trend", result=out,
                        extra={"reason": "empty_value_column",
                               "value_column": value_column,
                               "n_in_range": n_in_range,
                               "n_value_skipped": n_value_skipped})
            return out

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
        # Named `*_all_time` because these are the GLOBAL extremes over the
        # whole series. Called `peak`, it read as "the peak" and got quoted for
        # "recent spike" questions, pointing a year off — see `threshold` below,
        # which is what a recency question actually needs.
        "peak_all_time":   {"period": peak["period"],   "value": peak["value"]},
        "trough_all_time": {"period": trough["period"], "value": trough["value"]},
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

    # Threshold crossings. Without this, "did TSR spike recently / cross the
    # threshold?" is unanswerable from the summary: `peak` is the GLOBAL peak
    # (2024-09 on the case that surfaced this), `slope` reads as declining, and
    # the catalog's `risk_threshold` never appeared in the output at all — so a
    # genuine 2025 breach (Apr 27.4, May 20.2 against a threshold of 20) was
    # visible only to whoever eyeballed 18 raw series points AND already knew
    # the threshold. `latest_breach` is the one a "recent" question needs.
    thresh = _column_threshold(real_table, value_column)
    if thresh is not None:
        limit = thresh["value"]
        above = (thresh.get("direction") or "above") == "above"
        breached = [
            s for s in series
            if ((s["raw_value"] > limit) if above else (s["raw_value"] < limit))
        ]
        # EPISODES, not a flat period list. A flat list reads as a range, and on
        # a series that breaches, recovers, then breaches again it spans the
        # whole history: "2024-03 to 2025-05" was reported as the window for
        # "when TSR was reacting RECENTLY" when the recent reaction is
        # 2025-04..2025-05 and 2025-01..03 sat below the line. Its endpoints
        # were artifacts too — the first one moved with list truncation.
        #
        # Contiguity is adjacency within `series` (every present bucket, in
        # order), so a bucket that is present-but-not-breaching splits an
        # episode. A period MISSING from the data does not split it — see
        # `missing_periods`.
        idx_by_period = {s["period"]: i for i, s in enumerate(series)}
        episodes: list[dict] = []
        for s in breached:
            i = idx_by_period[s["period"]]
            if episodes and i == episodes[-1]["_last_idx"] + 1:
                ep = episodes[-1]
                ep["end"], ep["_last_idx"] = s["period"], i
                ep["n_periods"] += 1
                if s["raw_value"] > ep["_peak_raw"] if above else s["raw_value"] < ep["_peak_raw"]:
                    ep["_peak_raw"], ep["peak"] = s["raw_value"], {
                        "period": s["period"], "value": s["value"]}
            else:
                episodes.append({
                    "start": s["period"], "end": s["period"], "n_periods": 1,
                    "peak": {"period": s["period"], "value": s["value"]},
                    "_last_idx": i, "_peak_raw": s["raw_value"],
                })
        for ep in episodes:
            ep.pop("_last_idx", None)
            ep.pop("_peak_raw", None)

        summary["threshold"] = {
            "value": limit,
            "risky_when": f"{'>' if above else '<'} {limit}",
            "n_breaching_periods": len(breached),
            "n_episodes": len(episodes),
            # THE field for "recently": a start..end window, directly usable as
            # a date filter for pulling that period's transactions.
            "latest_episode": episodes[-1] if episodes else None,
            "episodes": episodes[-_MAX_BREACH_EPISODES:],
            "latest_breach": (
                {"period": breached[-1]["period"], "value": breached[-1]["value"]}
                if breached else None
            ),
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
    # Coarse grains (month / quarter / year) have inherently bounded bucket
    # counts — even a decade of months is ~120 buckets (~18 KB). Down-sampling
    # them drops INTERIOR periods that the specialist then can't narrate (the
    # "trend truncated to <month>" symptom) even though the chart, rebuilt from
    # the full KP series, stays complete. So NEVER down-sample a coarse-grain
    # series: emit it whole regardless of the char budget. Only day / week can
    # explode without bound, so those still fall back to uniform down-sampling.
    _coarse_grain = period in ("month", "quarter", "year")
    if len(out) > _TREND_MAX_CHARS and not _coarse_grain:
        # The series renders the chart, so DON'T cut a contiguous block — the
        # old `series[:keep]` kept the earliest half and silently dropped the
        # most-recent months (the chart lost its right end). Down-sample
        # uniformly, preserving the first & last bucket so the full date range
        # survives; shrink until it fits.
        target = n_buckets
        while len(out) > _TREND_MAX_CHARS and target > 2:
            target = max(2, int(target * 0.75))
            sampled = _even_sample(series, target)
            payload["series"] = sampled
            payload["series_note"] = (
                f"down-sampled to {len(sampled)} of {n_buckets} periods to fit the "
                "size cap — full date range preserved (first & last kept)"
            )
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


def _salvage_specs_list(raw) -> list | None:
    """Best-effort parse of a batch tool's JSON-list argument (``specs_json``).

    On safechain the tool-call arguments are text-parsed with no constrained
    decoding, so this argument arrives malformed intermittently — most often
    TRUNCATED (the confirmed failure: ``specs_json`` == "[", which
    ``json.loads`` rejects with "Expecting value: line 1 column 2"), sometimes
    already a parsed list, sometimes fence-wrapped. Recover whatever COMPLETE
    spec objects are present rather than erroring out (which let the specialist
    fabricate). Returns the list (possibly empty if nothing complete parsed), or
    None if the input isn't a list at all.
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    t = raw.strip()
    for fence in ("```json", "```"):
        if t.startswith(fence):
            t = t[len(fence):].lstrip("\n")
            break
    if t.endswith("```"):
        t = t[:-3].rstrip()
    try:
        v = json.loads(t)
        return v if isinstance(v, list) else None
    except (json.JSONDecodeError, ValueError):
        pass
    # Truncated list — collect the complete top-level {...} objects inside it.
    if not t.startswith("["):
        return None
    decoder = json.JSONDecoder()
    objs: list = []
    i, n = 1, len(t)
    while i < n:
        while i < n and t[i] in " \t\r\n,":
            i += 1
        if i >= n or t[i] == "]":
            break
        try:
            obj, end = decoder.raw_decode(t, i)
        except (json.JSONDecodeError, ValueError):
            break
        objs.append(obj)
        i = end
    return objs


_BATCH_TREND_EXAMPLE = (
    '[{"table_name":"model_scores","value_column":"credit_loss_prob",'
    '"time_column":"trans_month","period":"month","op":"max"},'
    '{"table_name":"model_scores","value_column":"tot_struct_risk_score",'
    '"time_column":"trans_month","period":"month","op":"max"}]'
)


def _unparseable_specs_directive(tool: str, raw, example: str) -> str:
    """Anti-fabrication error for a batch tool whose specs argument didn't parse.

    The neutral 'invalid_json' error let the specialist HALLUCINATE trend values
    around the failure. This says, loudly, that no data was produced and forbids
    inventing numbers — the specialist must re-call with a complete JSON list."""
    preview = (raw[:120] if isinstance(raw, str) else str(raw)[:120])
    return json.dumps({
        "error": "specs_unparseable",
        "message": (
            f"specs_json did not parse as a JSON list (looked truncated/"
            f"malformed: {preview!r}). {tool} did NOT run — you have NO data "
            "from this call."
        ),
        "REQUIRED": (
            "Do NOT state, estimate, or infer any values (peaks, troughs, "
            "trends) — with no tool result that is fabrication. Re-call "
            f"{tool} with a COMPLETE JSON list, e.g. {example} . If it still "
            "fails, report a data_gap — never invent numbers."
        ),
    }, indent=2)


def _batch_summarize_trend_impl(specs_json: str) -> str:
    _log_call("batch_summarize_trend", {
        "specs_json": specs_json[:1000] if isinstance(specs_json, str) else str(specs_json)[:1000]})
    specs = _salvage_specs_list(specs_json)
    # Empty/None → unrecoverable (e.g. the safechain-truncated "[" ). Return a
    # hard anti-fabrication directive instead of a neutral error the specialist
    # would answer AROUND with invented peaks.
    if not specs:
        out = _unparseable_specs_directive(
            "batch_summarize_trend", specs_json, _BATCH_TREND_EXAMPLE)
        _log_result("batch_summarize_trend", result=out,
                    extra={"reason": "specs_unparseable"})
        return out

    # Cap at 6 — each trend returns a per-period series + summary, so the
    # batched payload grows faster than batch_aggregate's. 6 covers the
    # typical "trend each indicator in a concept group" pattern in
    # modeling.md without blowing the per-tool size budget.
    results: list[dict[str, Any]] = []
    for idx, spec in enumerate(specs[:6]):
        if not isinstance(spec, dict):
            results.append({
                "index": idx,
                "error": "spec must be an object",
            })
            continue
        result = _summarize_trend_impl(
            table_name=str(spec.get("table_name") or spec.get("table") or ""),
            value_column=str(spec.get("value_column") or ""),
            time_column=str(spec.get("time_column") or ""),
            period=str(spec.get("period") or "month"),
            op=str(spec.get("op") or "sum"),
            filter_column=str(spec.get("filter_column") or ""),
            filter_value=str(spec.get("filter_value") or ""),
            filter_op=str(spec.get("filter_op") or "eq"),
            start_date=str(spec.get("start_date") or ""),
            end_date=str(spec.get("end_date") or ""),
        )
        # Echo the value_column so the caller can map result→indicator
        # without re-parsing the spec list (LLMs lose track of index
        # ordering across long messages).
        results.append({
            "index": idx,
            "value_column": str(spec.get("value_column") or ""),
            "result": result,
        })

    payload: dict[str, Any] = {"results": results}
    if len(specs) > 6:
        payload["truncated"] = f"ran first 6 of {len(specs)} requested trends"
    out = json.dumps(payload, indent=2, default=str)
    _log_result("batch_summarize_trend", result=out,
                extra={"n_requested": len(specs), "n_run": len(results)})
    return out


@function_tool
def batch_summarize_trend(specs_json: str) -> str:
    """Run multiple ``summarize_trend`` calls in ONE tool round-trip.

    Use when you need to trend SEVERAL indicators from the same table in
    the same window — the classic modeling case: "for each indicator in
    the internal-delinquency concept group, give me the monthly trajectory
    over trans_month." Doing this as separate ``summarize_trend`` calls
    costs N LLM round-trips (~3-6s each); batching collapses them into
    one. ALWAYS prefer this over a loop when you've already identified
    2+ indicators to trend.

    ``specs_json`` must be a JSON list. Each item accepts the same
    arguments as ``summarize_trend``:

    ``[{"table_name": "model_scores", "value_column": "times_30_dpd",
        "time_column": "trans_month", "period": "month", "op": "max"},
       {"table_name": "model_scores", "value_column": "tpf_internal_delinq_idx",
        "time_column": "trans_month", "period": "month", "op": "max"}]``

    Returns JSON ``{"results": [{"index": 0, "value_column": "...",
    "result": "<summarize_trend output>"}, ...]}``. The per-trend
    ``result`` is the same JSON document a single ``summarize_trend``
    call would have returned (summary + series).

    Cap: 6 specs per call (each trend carries a full per-period series).
    For 7+ indicators, split into two batches.
    """
    return _batch_summarize_trend_impl(specs_json)


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
            # Raw companion to the formatted string above. Renderers and any
            # share arithmetic need a NUMBER — parsing "$3,927,582.20" back out
            # is exactly the kind of transcription step that goes wrong.
            "total_across_groups_raw": (round(total_value, 4)
                                        if isinstance(total_value, float)
                                        else total_value),
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

    n_groups_returned = len(series)

    # Tail aggregate. Downstream consumers treat `groups` as THE series: the
    # auto-charter sums it for its "N% of total" claim and plots it as a share
    # bar. With a truncated top-N that denominator is the top-N sum, not the
    # whole — measured at top_n=5 of 40 groups, the claim read "20% of total"
    # where the true share was 3.1%. So when groups were dropped, say so IN the
    # series rather than only in `concentration`, whose total is a formatted
    # string a renderer can't do arithmetic with.
    #
    # Additive ops only: summing per-group means/maxes is meaningless, so a
    # tail there would invent a number rather than restore one.
    if additive and n_groups_total > n_groups_returned:
        rest = raw_per_group[top_n_int:]
        tail_value = sum(v for _, v, _ in rest)
        series.append({
            "group": format_tail_group(len(rest)),
            "value": _format_aggregate(tail_value, value_column, op),
            "raw_value": round(tail_value, 4) if isinstance(tail_value, float)
                         else tail_value,
            "n_records": sum(len(vals) for _, _, vals in rest),
            "is_tail": True,
        })

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
        # Counts the REAL groups, excluding any tail row appended above — the
        # tail is a remainder, not a group.
        "n_groups_returned": n_groups_returned,
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
