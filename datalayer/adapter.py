"""Schema reconciliation between real CSV data and the canonical catalog.

This module is invoked only at sync time (explicit trigger), never at query time.
It is the ONLY place in the codebase that imports pandas — the gateway and catalog
stay pure-Python. See tests/test_adapter.py::test_pandas_scope for enforcement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# Tunable thresholds — constants at module top so they're easy to find.
FUZZY_THRESHOLD = 0.85       # minimum difflib ratio to surface as candidate
TOP_K = 3                    # max candidates shown for an ambiguous column
DTYPE_COMPAT_THRESHOLD = 0.5 # min parse-success rate to call dtype "compatible"


@dataclass
class Candidate:
    """One potential canonical match for a real column."""
    canonical_table: str
    canonical_col: str
    ratio: float
    canonical_dtype: str
    dtype_compatible: bool


@dataclass
class ColumnDiff:
    """Result of matching one real column against the catalog."""
    real_table: str
    real_col: str
    real_dtype: str
    bucket: Literal["auto", "ambiguous", "new"]
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None
    parse_hint: str | None = None
    drafted_description: str = ""


@dataclass
class Diff:
    """Full diff for a case — the output of reconcile_case."""
    case_id: str
    auto_aliased: list[ColumnDiff] = field(default_factory=list)
    ambiguous: list[ColumnDiff] = field(default_factory=list)
    new: list[ColumnDiff] = field(default_factory=list)
    new_tables: list[str] = field(default_factory=list)


# ── Name normalization ─────────────────────────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9]")
_TRAILING_DIGITS = re.compile(r"\d+$")


def _normalize_name(name: str) -> str:
    """Normalize a column or table name for fuzzy comparison.

    Lowercase → strip non-alphanumerics → trim trailing digits.
    Idempotent: normalize(normalize(x)) == normalize(x).
    """
    lower = name.lower()
    alnum = _NON_ALNUM.sub("", lower)
    return _TRAILING_DIGITS.sub("", alnum)


# ── Dtype compatibility (sync-time, pandas-backed) ────────────────────────

import pandas as pd

_STRING_DTYPES = {"str", "string", "text", "category"}
_DATE_DTYPES = {"date", "datetime", "datetime64", "timestamp"}
_INT_DTYPES = {"int", "integer", "int64", "int32"}
_FLOAT_DTYPES = {"float", "float64", "float32", "number", "numeric"}

# Common date formats tried as fallback when mixed-mode parsing underperforms.
# Ordering matters: unambiguous formats first, ambiguous ones last.
_DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m",
    "%b'%Y",
    "%b %Y",
    "%B %Y",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
]


def _dtype_compatible(samples: list, canonical_dtype: str) -> bool:
    """Check if sample values could plausibly be of the canonical dtype.

    Strategy: try parsing with pandas coercion; require parse success rate
    >= DTYPE_COMPAT_THRESHOLD on non-null samples. Strings are always
    compatible (we can't rule them out without semantic knowledge).
    """
    canonical_dtype = canonical_dtype.lower()
    if canonical_dtype in _STRING_DTYPES:
        return True

    # Filter Nones/empties — if nothing left, no evidence to reject.
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return True

    series = pd.Series([str(s) for s in non_null])

    if canonical_dtype in _INT_DTYPES or canonical_dtype in _FLOAT_DTYPES:
        parsed = pd.to_numeric(series, errors="coerce")
        return bool(parsed.notna().mean() >= DTYPE_COMPAT_THRESHOLD)

    if canonical_dtype in _DATE_DTYPES:
        # Start with mixed-mode (handles most ISO / dateutil-parseable forms).
        best_rate = float(
            pd.to_datetime(series, errors="coerce", format="mixed").notna().mean()
        )
        # Fall back to explicit formats for exotic patterns (e.g., "Nov'2025").
        if best_rate < DTYPE_COMPAT_THRESHOLD:
            for fmt in _DATE_FORMATS:
                try:
                    rate = float(
                        pd.to_datetime(series, errors="coerce", format=fmt).notna().mean()
                    )
                except (ValueError, TypeError):
                    continue
                if rate > best_rate:
                    best_rate = rate
                    if best_rate >= DTYPE_COMPAT_THRESHOLD:
                        break
        return bool(best_rate >= DTYPE_COMPAT_THRESHOLD)

    # Unknown canonical dtype — don't reject.
    return True


# ── Four-stage matcher ─────────────────────────────────────────────────────

from difflib import SequenceMatcher


def _infer_real_dtype(samples: list) -> str:
    """Infer a loose dtype label for the real column from sample values."""
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return "unknown"
    series = pd.Series([str(s) for s in non_null])
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().mean() >= 0.95:
        return "int" if (numeric.dropna() % 1 == 0).all() else "float"
    parsed_date = pd.to_datetime(series, errors="coerce", format="mixed")
    if parsed_date.notna().mean() >= 0.95:
        return "date"
    # Try explicit date formats too — catches exotic patterns like "Nov'2025".
    for fmt in _DATE_FORMATS:
        try:
            rate = float(
                pd.to_datetime(series, errors="coerce", format=fmt).notna().mean()
            )
        except (ValueError, TypeError):
            continue
        if rate >= 0.95:
            return "date"
    return "string"


def match_column(
    real_table: str,
    real_col: str,
    real_samples: list,
    canonical: dict[str, dict[str, dict]],
) -> ColumnDiff:
    """Match a real CSV column against the canonical catalog.

    Returns a ColumnDiff with bucket in {"auto", "ambiguous", "new"}.
    """
    real_norm = _normalize_name(real_col)
    real_dtype_hint = _infer_real_dtype(real_samples)

    # Stage 1 — Exact match against canonical name or any alias.
    for canonical_table, cols in canonical.items():
        for canonical_col, spec in cols.items():
            aliases = spec.get("aliases", []) or []
            if real_col == canonical_col or real_col in aliases:
                chosen = Candidate(
                    canonical_table=canonical_table,
                    canonical_col=canonical_col,
                    ratio=1.0,
                    canonical_dtype=spec["dtype"],
                    dtype_compatible=True,
                )
                return ColumnDiff(
                    real_table=real_table,
                    real_col=real_col,
                    real_dtype=real_dtype_hint,
                    bucket="auto",
                    chosen=chosen,
                )

    # Stage 2 — Normalized match against canonical name or any alias.
    for canonical_table, cols in canonical.items():
        for canonical_col, spec in cols.items():
            aliases = spec.get("aliases", []) or []
            candidates_norm = {_normalize_name(canonical_col)} | {
                _normalize_name(a) for a in aliases
            }
            if real_norm in candidates_norm:
                dtype_ok = _dtype_compatible(real_samples, spec["dtype"])
                cand = Candidate(
                    canonical_table=canonical_table,
                    canonical_col=canonical_col,
                    ratio=1.0,
                    canonical_dtype=spec["dtype"],
                    dtype_compatible=dtype_ok,
                )
                if dtype_ok:
                    return ColumnDiff(
                        real_table=real_table,
                        real_col=real_col,
                        real_dtype=real_dtype_hint,
                        bucket="auto",
                        chosen=cand,
                    )
                return ColumnDiff(
                    real_table=real_table,
                    real_col=real_col,
                    real_dtype=real_dtype_hint,
                    bucket="ambiguous",
                    candidates=[cand],
                )

    # Stage 3 — Fuzzy match against canonical names.
    all_candidates: list[Candidate] = []
    for canonical_table, cols in canonical.items():
        for canonical_col, spec in cols.items():
            canonical_norm = _normalize_name(canonical_col)
            ratio = SequenceMatcher(None, real_norm, canonical_norm).ratio()
            if ratio >= FUZZY_THRESHOLD:
                all_candidates.append(Candidate(
                    canonical_table=canonical_table,
                    canonical_col=canonical_col,
                    ratio=ratio,
                    canonical_dtype=spec["dtype"],
                    dtype_compatible=_dtype_compatible(real_samples, spec["dtype"]),
                ))

    all_candidates.sort(key=lambda c: (-c.ratio, c.canonical_col))
    top = all_candidates[:TOP_K]

    if top:
        return ColumnDiff(
            real_table=real_table,
            real_col=real_col,
            real_dtype=real_dtype_hint,
            bucket="ambiguous",
            candidates=top,
        )

    # Stage 4 — Genuinely new.
    return ColumnDiff(
        real_table=real_table,
        real_col=real_col,
        real_dtype=real_dtype_hint,
        bucket="new",
    )


# ── Parse-hint inference (strptime pattern for date-as-string columns) ────

# Common-sense name → draft description patterns. First substring match wins.
_DRAFT_PATTERNS: list[tuple[str, str]] = [
    ("id", "unique identifier"),
    ("date", "date value — verify format and semantics"),
    ("amount", "monetary amount"),
    ("balance", "balance value"),
    ("count", "count of occurrences"),
    ("rate", "rate or ratio"),
    ("score", "score value"),
    ("name", "name string"),
    ("code", "code value — verify encoding"),
]


def _draft_description(col_name: str) -> str:
    """Propose a provisional description for an obviously-named new column.

    Returns empty string when no pattern matches — forcing the human to
    describe the column from scratch.
    """
    norm = _normalize_name(col_name)
    for keyword, draft in _DRAFT_PATTERNS:
        if keyword in norm:
            return f"{draft} (agent-drafted, unverified)"
    return ""


def _infer_parse_hint(samples: list) -> str | None:
    """Detect a strptime format pattern for date-as-string columns.

    Returns the format with the highest parse-success rate (>= threshold)
    from _DATE_FORMATS, or None if no format reaches the threshold.
    """
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return None

    series = pd.Series([str(s) for s in non_null])
    best: tuple[float, str] | None = None

    for fmt in _DATE_FORMATS:
        try:
            rate = float(
                pd.to_datetime(series, errors="coerce", format=fmt).notna().mean()
            )
        except (ValueError, TypeError):
            continue
        if rate >= DTYPE_COMPAT_THRESHOLD:
            if best is None or rate > best[0]:
                best = (rate, fmt)

    return best[1] if best else None


# ── Case reconciliation (orchestrates matcher over all tables/cols) ────────

def reconcile_case(gateway, canonical: dict, case_id: str) -> Diff:
    """Reconcile all tables+columns in a case against the canonical catalog.

    Pure function — does NOT write to YAML. Caller (apply_diff) handles I/O.
    """
    diff = Diff(case_id=case_id)
    gateway.set_case(case_id)

    canonical_tables = set(canonical.keys())

    for table in gateway.list_tables():
        rows = gateway.query(table) or []
        if table not in canonical_tables:
            diff.new_tables.append(table)

        if not rows:
            continue

        for col in rows[0].keys():
            samples = [r.get(col) for r in rows[:200]]

            result = match_column(
                real_table=table,
                real_col=col,
                real_samples=samples,
                canonical=canonical,
            )

            # Parse hint: only attempt for columns with stringy samples.
            if result.real_dtype == "string":
                hint = _infer_parse_hint(samples)
                if hint:
                    result.parse_hint = hint

            if result.bucket == "auto":
                diff.auto_aliased.append(result)
            elif result.bucket == "ambiguous":
                diff.ambiguous.append(result)
            else:
                result.drafted_description = _draft_description(col)
                diff.new.append(result)

    return diff


def _resolve_canonical_table_name(
    real_table: str,
    canonical_tables: list[str],
    catalog=None,
) -> str | None:
    """Find which canonical table a real table should fold into.

    Cascade: exact key → table-level aliases (when ``catalog`` is supplied)
    → equal under normalization → substring overlap of normalized forms.
    Returns None when nothing matches — caller treats the real table as a
    brand-new canonical entry.
    """
    if real_table in canonical_tables:
        return real_table
    if catalog is not None:
        for canonical in canonical_tables:
            profile = catalog._profiles.get(canonical, {}) or {}
            if real_table in (profile.get("aliases") or []):
                return canonical
    real_norm = _normalize_name(real_table)
    for canonical in canonical_tables:
        if _normalize_name(canonical) == real_norm:
            return canonical
    for canonical in canonical_tables:
        canonical_norm = _normalize_name(canonical)
        if canonical_norm and (canonical_norm in real_norm or real_norm in canonical_norm):
            return canonical
    return None


def apply_diff(diff: Diff, catalog) -> None:
    """Persist auto_aliased + new entries to the catalog's YAML profiles.

    New columns route via :func:`_resolve_canonical_table_name` so that a
    real table that maps to an existing canonical (e.g. ``bureau_data``
    ↔ ``bureau``) folds its new columns into that canonical's YAML rather
    than spawning a separate file. Real tables with no canonical match
    create a new canonical YAML keyed on the real name. Ambiguous entries
    are NOT persisted — they go to the caller for human review.
    """
    patches: dict[str, dict] = {}
    canonical_tables = list(catalog._profiles.keys())

    def _patch_for(table: str) -> dict:
        if table not in patches:
            patches[table] = {"columns": {}}
        return patches[table]

    for entry in diff.auto_aliased:
        if entry.chosen is None:
            continue
        if entry.real_col == entry.chosen.canonical_col:
            continue
        t = _patch_for(entry.chosen.canonical_table)
        col_patch = t["columns"].setdefault(entry.chosen.canonical_col, {})
        col_patch.setdefault("aliases", [])
        if entry.real_col not in col_patch["aliases"]:
            col_patch["aliases"].append(entry.real_col)

    for entry in diff.new:
        target_table = (
            _resolve_canonical_table_name(entry.real_table, canonical_tables, catalog=catalog)
            or entry.real_table
        )
        t = _patch_for(target_table)
        col_patch: dict = {
            "dtype": entry.real_dtype,
            "description": entry.drafted_description,
            "description_pending": True,
            "aliases": [entry.real_col],
        }
        if entry.parse_hint:
            col_patch["parse_hint"] = entry.parse_hint
        t["columns"][entry.real_col] = col_patch

    for table, patch in patches.items():
        catalog.write_profile_patch(table, patch)


# ── Multi-case aggregation ────────────────────────────────────────────────


@dataclass
class AggregatedDiff:
    """Catalog-wide diff aggregated across all scanned cases.

    Each list is deduped by (real_table, real_col) — an entry that recurs
    across cases is reported once. ``dtype_conflicts`` flags real columns
    whose inferred dtype differs across cases (rare; usually a data-quality
    smell worth surfacing).
    """
    case_count: int
    auto_aliased: list[ColumnDiff] = field(default_factory=list)
    ambiguous: list[ColumnDiff] = field(default_factory=list)
    new_columns: list[ColumnDiff] = field(default_factory=list)
    new_tables: list[str] = field(default_factory=list)
    dtype_conflicts: list[tuple[str, str, set[str]]] = field(default_factory=list)


def aggregate_diffs(diffs: list[Diff]) -> AggregatedDiff:
    """Merge per-case diffs into a single catalog-wide view.

    Dedup key is ``(real_table, real_col)``. The first occurrence wins; later
    occurrences are discarded but their ``real_dtype`` is checked against
    the first to detect conflicts.
    """
    seen_auto: dict[tuple[str, str], ColumnDiff] = {}
    seen_ambig: dict[tuple[str, str], ColumnDiff] = {}
    seen_new: dict[tuple[str, str], ColumnDiff] = {}
    seen_tables: set[str] = set()
    dtype_seen: dict[tuple[str, str], set[str]] = {}

    def _track_dtype(entry: ColumnDiff) -> None:
        key = (entry.real_table, entry.real_col)
        dtype_seen.setdefault(key, set()).add(entry.real_dtype)

    for d in diffs:
        for entry in d.auto_aliased:
            key = (entry.real_table, entry.real_col)
            _track_dtype(entry)
            seen_auto.setdefault(key, entry)
        for entry in d.ambiguous:
            key = (entry.real_table, entry.real_col)
            _track_dtype(entry)
            seen_ambig.setdefault(key, entry)
        for entry in d.new:
            key = (entry.real_table, entry.real_col)
            _track_dtype(entry)
            seen_new.setdefault(key, entry)
        for table in d.new_tables:
            seen_tables.add(table)

    conflicts = [
        (table, col, dtypes)
        for (table, col), dtypes in dtype_seen.items()
        if len(dtypes) > 1
    ]

    return AggregatedDiff(
        case_count=len(diffs),
        auto_aliased=list(seen_auto.values()),
        ambiguous=list(seen_ambig.values()),
        new_columns=list(seen_new.values()),
        new_tables=sorted(seen_tables),
        dtype_conflicts=conflicts,
    )


# ── Profile-only audit (canonical entries no real case has) ───────────────


@dataclass
class ProfileOnlyEntry:
    """A canonical column not observed in any scanned real case."""
    table: str
    column: str


@dataclass
class ProfileOnlyAudit:
    """Result of comparing the canonical catalog against observed real data."""
    profile_only_tables: list[str] = field(default_factory=list)
    profile_only_columns: list[ProfileOnlyEntry] = field(default_factory=list)


def audit_profile_only(
    catalog,
    observed: dict[str, set[str]],
) -> ProfileOnlyAudit:
    """Find canonical tables/columns that no scanned case observed.

    Parameters
    ----------
    catalog : DataCatalog
        The canonical catalog (reads ``_profiles``).
    observed : dict[str, set[str]]
        ``{real_table_name: {real_col_name, ...}}`` aggregated across all
        scanned real cases.

    A canonical table is considered "observed" if some real table's
    normalized name matches or substring-overlaps the canonical's
    normalized name. A canonical column is considered "observed" if its
    canonical name OR any of its aliases (normalized) appears in the
    union of observed columns from any matching real table.
    """
    audit = ProfileOnlyAudit()

    for canonical_table in catalog.list_tables():
        canonical_norm = _normalize_name(canonical_table)
        canonical_aliases = set(catalog._profiles[canonical_table].get("aliases") or [])
        matching_obs = [
            obs for obs in observed
            if (
                obs in canonical_aliases
                or _normalize_name(obs) == canonical_norm
                or canonical_norm in _normalize_name(obs)
                or _normalize_name(obs) in canonical_norm
            )
        ]

        if not matching_obs:
            audit.profile_only_tables.append(canonical_table)
            continue

        observed_cols: set[str] = set()
        for t in matching_obs:
            observed_cols |= observed[t]
        observed_norm = {_normalize_name(c) for c in observed_cols}

        cols = catalog._profiles[canonical_table].get("columns", {}) or {}
        for canonical_col, spec in cols.items():
            aliases = spec.get("aliases", []) or []
            names_norm = {_normalize_name(n) for n in {canonical_col, *aliases}}
            if not (names_norm & observed_norm):
                audit.profile_only_columns.append(
                    ProfileOnlyEntry(table=canonical_table, column=canonical_col)
                )

    return audit
