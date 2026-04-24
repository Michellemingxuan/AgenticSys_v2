# Data Catalog Sync Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. TDD: failing test → minimal implementation → passing test → commit.

**Goal:** Add a schema-adapter (`data/adapter.py`) and a reconciliation skill (`skills/workflow/data_catalog_sync.md`) so the data-manager agent can sync a real case folder's schema into the shared YAML catalog — auto-aliasing confident matches, surfacing ambiguous ones for human pick, and flagging genuinely new tables/columns with `description_pending: true` for human description.

**Architecture:** Pure reconciliation logic in `data/adapter.py` (imports pandas, sync-time only — never touches the query hot path). YAML profiles in `config/data_profiles/` gain three optional backwards-compatible fields (`aliases`, `description_pending`, `parse_hint`). Catalog gains a case-filtered view with `[UNVERIFIED]` annotations. Data-manager agent gains `sync_catalog(case_id)` and `verify_description(table, col)` methods.

**Tech Stack:** Python 3.11+ · Pydantic v2 (already a dep) · PyYAML (already a dep) · pandas (NEW dep, scoped to `data/adapter.py`) · difflib (stdlib) · pytest.

**Spec:** [docs/specs/2026-04-24-data-catalog-sync-design.md](../specs/2026-04-24-data-catalog-sync-design.md)

---

## File Structure

**Create:**
- `data/adapter.py` — pure reconciliation logic; imports pandas and stdlib only
- `skills/workflow/data_catalog_sync.md` — skill body for data-manager agent
- `tests/test_adapter.py` — unit tests for matcher + pandas-scope meta-test
- `tests/test_catalog_sync.py` — end-to-end integration test

**Modify:**
- `requirements.txt` — add `pandas>=2.0.0,<3.0.0`
- `data/catalog.py` — extend `to_prompt_context()` with case-filtered mode; add `write_profile_patch(table, patch)`
- `agents/data_manager_agent.py` — add `sync_catalog(case_id)` and `verify_description(table, col, new_text=None)`; update `describe_catalog()` to pass case-schema into catalog

**Not modified:** `data/gateway.py` — explicit architectural boundary (no pandas, no renames).

---

## Task 1: Scaffold — pandas dep + empty adapter + pandas-scope meta-test

**Files:**
- Modify: `requirements.txt`
- Create: `data/adapter.py`
- Create: `tests/test_adapter.py`

- [ ] **Step 1: Add pandas to requirements.txt**

Append this line (preserving existing ordering near numpy):

```
pandas>=2.0.0,<3.0.0
```

Run:

```bash
pip install "pandas>=2.0.0,<3.0.0"
```

Expected: install succeeds.

- [ ] **Step 2: Create `data/adapter.py` with module skeleton**

```python
"""Schema reconciliation between real CSV data and the canonical catalog.

This module is invoked only at sync time (explicit trigger), never at query time.
It is the ONLY place in the codebase that imports pandas — the gateway and catalog
stay pure-Python. See tests/test_adapter.py::test_pandas_scope for enforcement.
"""

from __future__ import annotations

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
    candidates: list[Candidate] = field(default_factory=list)  # empty for "new"
    chosen: Candidate | None = None  # populated for "auto"; None for "ambiguous"/"new"
    parse_hint: str | None = None    # populated for date-as-string columns
    drafted_description: str = ""    # populated for "new" with common-sense name


@dataclass
class Diff:
    """Full diff for a case — the output of reconcile_case."""
    case_id: str
    auto_aliased: list[ColumnDiff] = field(default_factory=list)
    ambiguous: list[ColumnDiff] = field(default_factory=list)
    new: list[ColumnDiff] = field(default_factory=list)
    new_tables: list[str] = field(default_factory=list)
```

- [ ] **Step 3: Create `tests/test_adapter.py` with the pandas-scope meta-test**

```python
"""Tests for data.adapter — the sync-time schema reconciler."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from data import adapter


REPO_ROOT = Path(__file__).parent.parent
SCOPE_GUARDED_PATHS = [
    "data/gateway.py",
    "data/catalog.py",
    "agents",
    "tools",
]


def test_adapter_module_importable():
    """Smoke test: module imports and constants are defined."""
    assert adapter.FUZZY_THRESHOLD == 0.85
    assert adapter.TOP_K == 3
    assert adapter.DTYPE_COMPAT_THRESHOLD == 0.5


def test_pandas_scope():
    """pandas must ONLY be imported inside data/adapter.py — never by gateway,
    catalog, agents, or tools. Enforced via grep over the guarded paths.
    """
    for rel in SCOPE_GUARDED_PATHS:
        target = REPO_ROOT / rel
        # Use `git grep` if available, fall back to `grep -r`
        cmd = ["grep", "-rn", "--include=*.py", "import pandas", str(target)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        # grep exit 1 = no matches (the good outcome); exit 0 = found matches (fail)
        assert result.returncode == 1, (
            f"pandas import leaked into {rel}:\n{result.stdout}"
        )
```

- [ ] **Step 4: Run the tests to verify the scaffold passes**

```bash
pytest tests/test_adapter.py -v
```

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt data/adapter.py tests/test_adapter.py
git commit -m "feat(adapter): scaffold module + pandas dep + scope guard test"
```

---

## Task 2: `_normalize_name` helper

**Files:**
- Modify: `data/adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing tests for `_normalize_name`**

Append to `tests/test_adapter.py`:

```python
@pytest.mark.parametrize("raw,expected", [
    ("fico_score", "ficoscore"),
    ("FICO_Score", "ficoscore"),
    ("trans-amt", "transamt"),
    ("Trans.Amt", "transamt"),
    ("amount_v2", "amountv"),       # non-alnums stripped, trailing digits trimmed
    ("col_123", "col"),             # trailing digits stripped (cleanly numeric tail)
    ("", ""),
    ("a", "a"),
    ("already_normalized_no_change", "alreadynormalizednochange"),
])
def test_normalize_name(raw, expected):
    assert adapter._normalize_name(raw) == expected


def test_normalize_name_idempotent():
    """Applying normalize twice yields the same result as applying once."""
    for raw in ["fico_score", "TRANS-AMT", "amount_v2", ""]:
        once = adapter._normalize_name(raw)
        twice = adapter._normalize_name(once)
        assert once == twice
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_adapter.py::test_normalize_name -v
```

Expected: FAIL with `AttributeError: module 'data.adapter' has no attribute '_normalize_name'`.

- [ ] **Step 3: Implement `_normalize_name`**

Append to `data/adapter.py`:

```python
import re

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
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_adapter.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add data/adapter.py tests/test_adapter.py
git commit -m "feat(adapter): add _normalize_name for lightweight fuzzy matching"
```

---

## Task 3: `_dtype_compatible` — pandas-powered parse-rate check

**Files:**
- Modify: `data/adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing tests for `_dtype_compatible`**

Append to `tests/test_adapter.py`:

```python
@pytest.mark.parametrize("samples,canonical_dtype,expected", [
    # Integer-like strings → compatible with int
    (["1", "2", "3"], "int", True),
    (["1", "two", "3"], "int", False),  # <50% parse rate
    # Float-like → compatible with float
    (["1.5", "2.0", "3.14"], "float", True),
    (["abc", "def"], "float", False),
    # Date-like strings → compatible with date
    (["2025-01-01", "2025-02-15", "2025-12-31"], "date", True),
    (["Nov'2025", "Dec'2025", "Jan'2026"], "date", True),
    (["not a date", "also not"], "date", False),
    # String canonical accepts anything
    (["anything", "goes"], "str", True),
    (["123", "456"], "string", True),
    # Empty samples → treated as compatible (no evidence against)
    ([], "int", True),
    ([None, None], "int", True),  # all None = no testable samples
])
def test_dtype_compatible(samples, canonical_dtype, expected):
    assert adapter._dtype_compatible(samples, canonical_dtype) is expected
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_adapter.py::test_dtype_compatible -v
```

Expected: FAIL with `AttributeError: ... '_dtype_compatible'`.

- [ ] **Step 3: Implement `_dtype_compatible`**

Append to `data/adapter.py`:

```python
import pandas as pd


_STRING_DTYPES = {"str", "string", "text", "category"}
_DATE_DTYPES = {"date", "datetime", "datetime64", "timestamp"}
_INT_DTYPES = {"int", "integer", "int64", "int32"}
_FLOAT_DTYPES = {"float", "float64", "float32", "number", "numeric"}


def _dtype_compatible(samples: list, canonical_dtype: str) -> bool:
    """Check if sample values could plausibly be of the canonical dtype.

    Strategy: try parsing with pandas coercion; require parse success rate
    >= DTYPE_COMPAT_THRESHOLD on non-null samples. Strings are always
    compatible (we can't rule them out without semantic knowledge).
    """
    canonical_dtype = canonical_dtype.lower()
    if canonical_dtype in _STRING_DTYPES:
        return True

    # Filter Nones — if nothing left, we have no evidence to reject.
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return True

    series = pd.Series(non_null)

    if canonical_dtype in _INT_DTYPES or canonical_dtype in _FLOAT_DTYPES:
        parsed = pd.to_numeric(series, errors="coerce")
    elif canonical_dtype in _DATE_DTYPES:
        parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    else:
        # Unknown canonical dtype → don't reject.
        return True

    success_rate = parsed.notna().mean()
    return bool(success_rate >= DTYPE_COMPAT_THRESHOLD)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_adapter.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add data/adapter.py tests/test_adapter.py
git commit -m "feat(adapter): add _dtype_compatible using pandas coercion"
```

---

## Task 4: `match_column` — four-stage matcher

**Files:**
- Modify: `data/adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing tests for `match_column`**

Append to `tests/test_adapter.py`:

```python
def _canonical_fixture() -> dict[str, dict[str, dict]]:
    """Minimal canonical catalog fixture for match_column tests."""
    return {
        "transactions": {
            "amount": {
                "dtype": "float",
                "aliases": ["trans_amt"],
            },
            "transaction_date": {
                "dtype": "date",
                "aliases": [],
            },
            "mcc": {
                "dtype": "str",
                "aliases": [],
            },
        },
        "bureau": {
            "fico_score": {
                "dtype": "int",
                "aliases": ["fico"],
            },
        },
    }


def test_match_exact_canonical_name_is_auto():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="bureau",
        real_col="fico_score",
        real_samples=["700", "720", "680"],
        canonical=catalog,
    )
    assert result.bucket == "auto"
    assert result.chosen is not None
    assert result.chosen.canonical_col == "fico_score"


def test_match_known_alias_is_auto():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="bureau",
        real_col="fico",  # listed as alias of fico_score
        real_samples=["700", "720"],
        canonical=catalog,
    )
    assert result.bucket == "auto"
    assert result.chosen.canonical_col == "fico_score"


def test_match_normalized_is_auto_when_dtype_compatible():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="transactions",
        real_col="MCC",  # normalizes to mcc
        real_samples=["5411", "5812"],
        canonical=catalog,
    )
    assert result.bucket == "auto"
    assert result.chosen.canonical_col == "mcc"


def test_match_fuzzy_is_ambiguous():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="transactions",
        real_col="transaction_dt",  # fuzzy-close to transaction_date
        real_samples=["2025-01-01", "2025-02-01"],
        canonical=catalog,
    )
    assert result.bucket == "ambiguous"
    assert any(c.canonical_col == "transaction_date" for c in result.candidates)
    assert len(result.candidates) <= adapter.TOP_K


def test_match_no_candidates_is_new():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="misc",
        real_col="totally_unrelated_field",
        real_samples=["a", "b"],
        canonical=catalog,
    )
    assert result.bucket == "new"
    assert result.candidates == []


def test_ambiguous_candidates_sorted_by_ratio_desc():
    catalog = _canonical_fixture()
    result = adapter.match_column(
        real_table="transactions",
        real_col="trans_date",
        real_samples=["2025-01-01"],
        canonical=catalog,
    )
    if len(result.candidates) >= 2:
        ratios = [c.ratio for c in result.candidates]
        assert ratios == sorted(ratios, reverse=True)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_adapter.py::test_match_exact_canonical_name_is_auto -v
```

Expected: FAIL with `AttributeError: ... 'match_column'`.

- [ ] **Step 3: Implement `match_column`**

Append to `data/adapter.py`:

```python
from difflib import SequenceMatcher


def match_column(
    real_table: str,
    real_col: str,
    real_samples: list,
    canonical: dict[str, dict[str, dict]],
) -> ColumnDiff:
    """Match a real CSV column against the canonical catalog.

    Parameters
    ----------
    real_table : str
        Table name as it appears in the CSV folder.
    real_col : str
        Column name as it appears in the CSV header.
    real_samples : list
        Sample values from the column, used for dtype compatibility checks.
    canonical : dict
        {table: {col: {"dtype": str, "aliases": list[str], ...}}} — the
        current catalog state.

    Returns
    -------
    ColumnDiff with bucket in {"auto", "ambiguous", "new"}.
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
                    dtype_compatible=True,  # exact match — trust prior human decisions
                )
                return ColumnDiff(
                    real_table=real_table,
                    real_col=real_col,
                    real_dtype=real_dtype_hint,
                    bucket="auto",
                    chosen=chosen,
                )

    # Stage 2 — Normalized match.
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
                # Normalized hit with dtype mismatch → ambiguous.
                return ColumnDiff(
                    real_table=real_table,
                    real_col=real_col,
                    real_dtype=real_dtype_hint,
                    bucket="ambiguous",
                    candidates=[cand],
                )

    # Stage 3 — Fuzzy match. Collect all candidates with ratio >= FUZZY_THRESHOLD.
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

    # Stage 4 — No candidates. Genuinely new.
    return ColumnDiff(
        real_table=real_table,
        real_col=real_col,
        real_dtype=real_dtype_hint,
        bucket="new",
    )


def _infer_real_dtype(samples: list) -> str:
    """Infer a loose dtype label for the real column from sample values."""
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return "unknown"
    series = pd.Series(non_null)
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().mean() >= 0.95:
        return "int" if (numeric.dropna() % 1 == 0).all() else "float"
    parsed_date = pd.to_datetime(series, errors="coerce", format="mixed")
    if parsed_date.notna().mean() >= 0.95:
        return "date"
    return "string"
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_adapter.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add data/adapter.py tests/test_adapter.py
git commit -m "feat(adapter): add four-stage match_column with dtype signal"
```

---

## Task 5: `_infer_parse_hint` for date-as-string columns

**Files:**
- Modify: `data/adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing tests for `_infer_parse_hint`**

Append to `tests/test_adapter.py`:

```python
@pytest.mark.parametrize("samples,expected", [
    (["2025-01-01", "2025-02-15", "2025-12-31"], "%Y-%m-%d"),
    (["01/15/2025", "12/31/2025", "03/04/2026"], "%m/%d/%Y"),
    (["15/01/2025", "31/12/2025", "04/03/2026"], "%d/%m/%Y"),
    (["Nov'2025", "Dec'2025", "Jan'2026"], "%b'%Y"),
    (["2025-01", "2025-02", "2025-12"], "%Y-%m"),
    (["not a date", "also nope"], None),
    ([], None),
    (["123", "456"], None),  # numeric, not dates
])
def test_infer_parse_hint(samples, expected):
    assert adapter._infer_parse_hint(samples) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_adapter.py::test_infer_parse_hint -v
```

Expected: FAIL with `AttributeError: ... '_infer_parse_hint'`.

- [ ] **Step 3: Implement `_infer_parse_hint`**

Append to `data/adapter.py`:

```python
# Common date formats tried in order. First format whose parse rate meets
# DTYPE_COMPAT_THRESHOLD wins. Ordering matters: unambiguous formats first.
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


def _infer_parse_hint(samples: list) -> str | None:
    """Detect a strptime format pattern for date-as-string columns.

    Returns the first format whose parse-success rate meets
    DTYPE_COMPAT_THRESHOLD on the non-null samples, or None if no format
    reaches the threshold.
    """
    non_null = [s for s in samples if s is not None and s != ""]
    if not non_null:
        return None

    series = pd.Series([str(s) for s in non_null])
    best: tuple[float, str] | None = None

    for fmt in _DATE_FORMATS:
        parsed = pd.to_datetime(series, errors="coerce", format=fmt)
        rate = parsed.notna().mean()
        if rate >= DTYPE_COMPAT_THRESHOLD:
            if best is None or rate > best[0]:
                best = (rate, fmt)

    return best[1] if best else None
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_adapter.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add data/adapter.py tests/test_adapter.py
git commit -m "feat(adapter): add _infer_parse_hint for date-as-string columns"
```

---

## Task 6: `reconcile_case` — pure diff producer (no I/O)

**Files:**
- Modify: `data/adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing test for `reconcile_case`**

Append to `tests/test_adapter.py`:

```python
def test_reconcile_case_produces_three_buckets():
    """Full end-to-end reconciliation of one case against a tiny catalog."""
    from data.gateway import SimulatedDataGateway

    # Case data: one table with 3 columns — one known-alias, one fuzzy, one new.
    case_data = {
        "case_A": {
            "transactions": [
                # Column "trans_amt" is a known alias of amount (auto).
                # Column "transaction_dt" is fuzzy-close to transaction_date (ambiguous).
                # Column "totally_new_field" has no match (new).
                {"trans_amt": "12.50", "transaction_dt": "2025-01-01", "totally_new_field": "x"},
                {"trans_amt": "30.00", "transaction_dt": "2025-02-15", "totally_new_field": "y"},
            ],
        },
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    gateway.set_case("case_A")

    canonical = _canonical_fixture()

    diff = adapter.reconcile_case(gateway, canonical, "case_A")

    # Expect exactly one entry in each bucket.
    assert diff.case_id == "case_A"
    assert len(diff.auto_aliased) == 1
    assert diff.auto_aliased[0].real_col == "trans_amt"
    assert diff.auto_aliased[0].chosen.canonical_col == "amount"

    assert len(diff.ambiguous) == 1
    assert diff.ambiguous[0].real_col == "transaction_dt"

    assert len(diff.new) == 1
    assert diff.new[0].real_col == "totally_new_field"


def test_reconcile_case_flags_unknown_tables():
    """A table present in the case but not in the canonical catalog lands in new_tables."""
    from data.gateway import SimulatedDataGateway

    case_data = {
        "case_A": {
            "brand_new_table": [{"foo": "1"}, {"foo": "2"}],
        },
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    gateway.set_case("case_A")
    canonical = _canonical_fixture()

    diff = adapter.reconcile_case(gateway, canonical, "case_A")

    assert "brand_new_table" in diff.new_tables
    # Its columns still go through matching and land in `new`.
    assert any(c.real_table == "brand_new_table" for c in diff.new)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_adapter.py::test_reconcile_case_produces_three_buckets -v
```

Expected: FAIL with `AttributeError: ... 'reconcile_case'`.

- [ ] **Step 3: Implement `reconcile_case`**

Append to `data/adapter.py`:

```python
# Common-sense name → draft description patterns. Matched against the
# normalized column name; first match wins.
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

        # Columns are the keys of the first row (all rows share schema per CSV).
        for col in rows[0].keys():
            samples = [r.get(col) for r in rows[:200]]  # cap sample size for speed

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
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_adapter.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add data/adapter.py tests/test_adapter.py
git commit -m "feat(adapter): add reconcile_case pure diff producer"
```

---

## Task 7: `catalog.write_profile_patch` + `adapter.apply_diff`

**Files:**
- Modify: `data/catalog.py`
- Modify: `data/adapter.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing test for `write_profile_patch` round-trip**

Append to `tests/test_adapter.py`:

```python
def test_write_profile_patch_appends_alias(tmp_path):
    """Round-trip: write_profile_patch appends an alias, reload confirms it."""
    from data.catalog import DataCatalog

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    initial = """\
table: bureau
description: "Bureau data"
columns:
  fico_score:
    dtype: int
    description: "FICO score"
    aliases: []
"""
    (profile_dir / "bureau.yaml").write_text(initial)

    cat = DataCatalog(profile_dir=str(profile_dir))
    cat.write_profile_patch("bureau", {
        "columns": {
            "fico_score": {"aliases": ["fico"]},
        },
    })

    # Reload a fresh catalog and confirm the alias is present.
    cat2 = DataCatalog(profile_dir=str(profile_dir))
    schema = cat2._profiles["bureau"]["columns"]["fico_score"]
    assert "fico" in schema["aliases"]


def test_write_profile_patch_adds_new_column(tmp_path):
    from data.catalog import DataCatalog

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    initial = """\
table: bureau
description: "Bureau data"
columns:
  fico_score:
    dtype: int
    description: "FICO score"
    aliases: []
"""
    (profile_dir / "bureau.yaml").write_text(initial)

    cat = DataCatalog(profile_dir=str(profile_dir))
    cat.write_profile_patch("bureau", {
        "columns": {
            "new_field": {
                "dtype": "string",
                "description": "",
                "description_pending": True,
                "aliases": ["new_field"],
            },
        },
    })

    cat2 = DataCatalog(profile_dir=str(profile_dir))
    new = cat2._profiles["bureau"]["columns"]["new_field"]
    assert new["description_pending"] is True
    assert new["aliases"] == ["new_field"]


def test_apply_diff_writes_auto_and_new_not_ambiguous(tmp_path):
    """apply_diff persists auto_aliased + new entries; leaves ambiguous alone."""
    from data.catalog import DataCatalog

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "transactions.yaml").write_text("""\
table: transactions
description: "Transaction data"
columns:
  amount:
    dtype: float
    description: "Transaction amount"
    aliases: [trans_amt]
  transaction_date:
    dtype: date
    description: "Transaction date"
    aliases: []
""")

    cat = DataCatalog(profile_dir=str(profile_dir))

    from data.gateway import SimulatedDataGateway
    case_data = {
        "case_A": {
            "transactions": [
                {"trans_amt": "12.50", "transaction_dt": "2025-01-01", "new_col": "x"},
                {"trans_amt": "30.00", "transaction_dt": "2025-02-15", "new_col": "y"},
            ],
        },
    }
    gateway = SimulatedDataGateway(case_data=case_data)
    canonical = {
        t: p["columns"] for t, p in cat._profiles.items()
    }
    diff = adapter.reconcile_case(gateway, canonical, "case_A")

    adapter.apply_diff(diff, cat)

    # Reload and check:
    cat2 = DataCatalog(profile_dir=str(profile_dir))
    trans_cols = cat2._profiles["transactions"]["columns"]
    # Auto: trans_amt was already an alias; no change expected.
    assert "trans_amt" in trans_cols["amount"]["aliases"]
    # Ambiguous (transaction_dt) was NOT written.
    assert "transaction_dt" not in trans_cols.get("transaction_date", {}).get("aliases", [])
    # New (new_col) was written with description_pending=true.
    assert "new_col" in trans_cols
    assert trans_cols["new_col"]["description_pending"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_adapter.py::test_write_profile_patch_appends_alias -v
```

Expected: FAIL with `AttributeError: 'DataCatalog' object has no attribute 'write_profile_patch'`.

- [ ] **Step 3: Implement `DataCatalog.write_profile_patch`**

Add to `data/catalog.py` (inside the `DataCatalog` class):

```python
    def write_profile_patch(self, table: str, patch: dict) -> None:
        """Merge a patch dict into the table's YAML profile and persist.

        Creates the profile file if the table doesn't exist yet. Appends
        to list-valued fields (e.g., aliases) instead of overwriting. Scalar
        fields are overwritten only if the patch provides a non-None value.

        After writing, the in-memory _profiles dict is refreshed from disk
        so the catalog stays consistent.
        """
        profile_path = self._profile_dir / f"{table}.yaml"

        if profile_path.exists():
            with open(profile_path) as f:
                profile = yaml.safe_load(f) or {}
        else:
            profile = {"table": table, "description": "", "columns": {}}

        self._merge_patch(profile, patch)

        with open(profile_path, "w") as f:
            yaml.safe_dump(profile, f, default_flow_style=False, sort_keys=False)

        # Refresh in-memory state.
        self._profiles[table] = profile

    @staticmethod
    def _merge_patch(base: dict, patch: dict) -> None:
        """Recursive merge. Lists are union-appended (dedup preserving order)."""
        for key, value in patch.items():
            if key not in base:
                base[key] = value
                continue
            existing = base[key]
            if isinstance(existing, dict) and isinstance(value, dict):
                DataCatalog._merge_patch(existing, value)
            elif isinstance(existing, list) and isinstance(value, list):
                for item in value:
                    if item not in existing:
                        existing.append(item)
            else:
                base[key] = value
```

- [ ] **Step 4: Implement `adapter.apply_diff`**

Append to `data/adapter.py`:

```python
def apply_diff(diff: Diff, catalog) -> None:
    """Persist auto_aliased + new entries to the catalog's YAML profiles.

    Does NOT persist ambiguous entries — those are returned to the caller
    for human review.
    """
    # Group patches by table.
    patches: dict[str, dict] = {}

    def _patch_for(table: str) -> dict:
        if table not in patches:
            patches[table] = {"columns": {}}
        return patches[table]

    for entry in diff.auto_aliased:
        if entry.chosen is None:
            continue
        t = _patch_for(entry.chosen.canonical_table)
        col_patch = t["columns"].setdefault(entry.chosen.canonical_col, {})
        col_patch.setdefault("aliases", [])
        if entry.real_col not in col_patch["aliases"]:
            col_patch["aliases"].append(entry.real_col)

    for entry in diff.new:
        t = _patch_for(entry.real_table)
        col_patch = {
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
```

- [ ] **Step 5: Run tests to verify pass**

```bash
pytest tests/test_adapter.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add data/catalog.py data/adapter.py tests/test_adapter.py
git commit -m "feat(catalog,adapter): write_profile_patch + apply_diff"
```

---

## Task 8: `catalog.to_prompt_context` — case-filtered view with [UNVERIFIED]

**Files:**
- Modify: `data/catalog.py`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing tests for case-filtered rendering**

Append to `tests/test_adapter.py`:

```python
def test_to_prompt_context_full_is_unchanged_by_default(tmp_path):
    """Default no-arg call preserves existing behavior (backwards compat)."""
    from data.catalog import DataCatalog

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "bureau.yaml").write_text("""\
table: bureau
description: "Bureau data"
columns:
  fico_score:
    dtype: int
    description: "FICO score"
""")

    cat = DataCatalog(profile_dir=str(profile_dir))
    out = cat.to_prompt_context()
    assert "TABLE: bureau" in out
    assert "fico_score" in out
    assert "[UNVERIFIED]" not in out  # no pending entries


def test_to_prompt_context_case_filtered(tmp_path):
    """case_schema filters the catalog to only tables present in the case."""
    from data.catalog import DataCatalog

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "bureau.yaml").write_text("""\
table: bureau
description: "Bureau data"
columns:
  fico_score:
    dtype: int
    description: "FICO score"
""")
    (profile_dir / "payments.yaml").write_text("""\
table: payments
description: "Payment data"
columns:
  amount:
    dtype: float
    description: "Payment amount"
""")

    cat = DataCatalog(profile_dir=str(profile_dir))
    # Case only has "bureau" — "payments" should be filtered out.
    out = cat.to_prompt_context(case_schema={"bureau": ["fico_score"]})

    assert "bureau" in out
    assert "payments" not in out


def test_to_prompt_context_unverified_marker_and_banner(tmp_path):
    """Pending columns show [UNVERIFIED] + case emits warning banner."""
    from data.catalog import DataCatalog

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "bureau.yaml").write_text("""\
table: bureau
description: "Bureau data"
columns:
  fico_score:
    dtype: int
    description: "FICO score"
    description_pending: false
  new_col:
    dtype: string
    description: "draft"
    description_pending: true
""")

    cat = DataCatalog(profile_dir=str(profile_dir))
    out = cat.to_prompt_context(case_schema={"bureau": ["fico_score", "new_col"]})
    assert "[UNVERIFIED]" in out
    assert "unverified descriptions" in out.lower()  # banner
    # fico_score should NOT carry the marker
    fico_line = next(line for line in out.splitlines() if "fico_score" in line)
    assert "[UNVERIFIED]" not in fico_line


def test_to_prompt_context_canonical_annotation_when_real_differs(tmp_path):
    """When real column name differs from canonical, [canonical: X] is added."""
    from data.catalog import DataCatalog

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "transactions.yaml").write_text("""\
table: transactions
description: "Transactions"
columns:
  amount:
    dtype: float
    description: "Transaction amount"
    aliases: [trans_amt]
""")

    cat = DataCatalog(profile_dir=str(profile_dir))
    # Real case uses the alias "trans_amt"
    out = cat.to_prompt_context(case_schema={"transactions": ["trans_amt"]})
    assert "trans_amt" in out
    assert "[canonical: amount]" in out
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_adapter.py::test_to_prompt_context_case_filtered -v
```

Expected: FAIL (existing `to_prompt_context` takes no args).

- [ ] **Step 3: Rewrite `DataCatalog.to_prompt_context`**

Replace the existing method in `data/catalog.py` with:

```python
    def to_prompt_context(
        self,
        case_schema: dict[str, list[str]] | None = None,
    ) -> str:
        """Format the catalog as text for injection into LLM prompts.

        Parameters
        ----------
        case_schema : dict[str, list[str]] | None
            Optional per-case filter: {table_name: [real_column_names]}.
            When provided, output includes only tables that are physically
            present in this case, and renders columns using their real
            names (annotated with [canonical: X] when they differ). When
            None, renders the full catalog using canonical names only
            (backwards-compatible with pre-sync behavior).
        """
        lines: list[str] = ["=== DATA CATALOG ===", ""]

        # Collect any pending flags in scope so we can emit a banner.
        scope_has_pending = False
        scope_tables = (
            [t for t in self.list_tables() if t in case_schema]
            if case_schema is not None
            else self.list_tables()
        )

        # Pre-scan for banner (only for the case-filtered branch).
        if case_schema is not None:
            for table in scope_tables:
                profile = self._profiles[table]
                for real_col in case_schema[table]:
                    spec = self._find_column_spec(profile, real_col)
                    if spec and spec.get("description_pending"):
                        scope_has_pending = True
                        break
                if scope_has_pending:
                    break

        if scope_has_pending:
            lines.append(
                "⚠ Some columns in this case have unverified descriptions — "
                "treat them cautiously."
            )
            lines.append("")

        for table in scope_tables:
            profile = self._profiles[table]
            desc = profile.get("description", "")
            lines.append(f"TABLE: {table}")
            lines.append(f"  {desc}")

            if case_schema is not None:
                real_cols = case_schema[table]
                for real_col in real_cols:
                    spec = self._find_column_spec(profile, real_col)
                    if spec is None:
                        # Column is in the case but not in any alias list —
                        # should not happen after a full sync, but render
                        # something sensible.
                        lines.append(f"  - {real_col} [unknown]: (not in catalog)")
                        continue
                    canonical = self._canonical_of(profile, real_col)
                    lines.append(self._format_column_line(
                        real_col=real_col,
                        canonical_col=canonical,
                        spec=spec,
                    ))
            else:
                for col, spec in profile.get("columns", {}).items():
                    lines.append(self._format_column_line(
                        real_col=col,
                        canonical_col=col,
                        spec=spec,
                    ))
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _find_column_spec(profile: dict, real_col: str) -> dict | None:
        """Find the column spec for a real name, checking canonical name and aliases."""
        columns = profile.get("columns", {})
        if real_col in columns:
            return columns[real_col]
        for spec in columns.values():
            if real_col in (spec.get("aliases") or []):
                return spec
        return None

    @staticmethod
    def _canonical_of(profile: dict, real_col: str) -> str:
        """Return the canonical column name that matches a real column name."""
        columns = profile.get("columns", {})
        if real_col in columns:
            return real_col
        for canonical, spec in columns.items():
            if real_col in (spec.get("aliases") or []):
                return canonical
        return real_col

    @staticmethod
    def _format_column_line(real_col: str, canonical_col: str, spec: dict) -> str:
        dtype = spec.get("dtype", "unknown")
        desc = spec.get("description", "")
        pending = spec.get("description_pending", False)
        parse_hint = spec.get("parse_hint")

        canonical_annot = (
            f" [canonical: {canonical_col}]"
            if real_col != canonical_col
            else ""
        )
        parse_annot = f" [parse: {parse_hint}]" if parse_hint else ""
        pending_annot = " [UNVERIFIED]" if pending else ""
        desc_str = f'"{desc}"' if desc else '(no description)'

        return (
            f"  - {real_col} ({dtype}){canonical_annot}: "
            f"{desc_str}{parse_annot}{pending_annot}"
        )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_adapter.py -v
```

Expected: all PASS.

Also run the existing catalog tests (if any) to confirm no regression:

```bash
pytest tests/ -v -k "catalog or describe" --tb=short
```

Expected: no failures introduced.

- [ ] **Step 5: Commit**

```bash
git add data/catalog.py tests/test_adapter.py
git commit -m "feat(catalog): case-filtered to_prompt_context with [UNVERIFIED] markers"
```

---

## Task 9: Create `skills/workflow/data_catalog_sync.md`

**Files:**
- Create: `skills/workflow/data_catalog_sync.md`
- Modify: `tests/test_adapter.py`

- [ ] **Step 1: Write failing test that loads the skill via the existing loader**

Append to `tests/test_adapter.py`:

```python
def test_data_catalog_sync_skill_loads():
    """The new sync skill file is parseable by the existing loader."""
    from pathlib import Path
    from skills.loader import load_skill

    skill_path = (
        Path(__file__).parent.parent
        / "skills" / "workflow" / "data_catalog_sync.md"
    )
    skill = load_skill(skill_path)
    assert skill.name
    assert "sync" in skill.name.lower() or "catalog" in skill.name.lower()
    # Body must mention the three buckets by name + the tool names.
    body_lower = skill.body.lower()
    assert "auto" in body_lower
    assert "ambiguous" in body_lower
    assert "new" in body_lower
    assert "sync_catalog" in body_lower
    assert "verify_description" in body_lower
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_adapter.py::test_data_catalog_sync_skill_loads -v
```

Expected: FAIL with `FileNotFoundError` or similar.

- [ ] **Step 3: Create the skill file**

Create `skills/workflow/data_catalog_sync.md`:

```markdown
---
name: Data Catalog Sync
description: Reconcile a real case folder's schema against the shared data catalog — auto-alias confident matches, surface ambiguous ones for human pick, flag genuinely new tables/columns with description_pending
type: workflow
owner: [data_manager]
mode: inline
tools: [sync_catalog, verify_description]
---

# Purpose

When a real case folder (`data_tables/<case>/*.csv`) is loaded, its table and column names may not exactly match the canonical catalog in `config/data_profiles/*.yaml`. This skill defines the procedure the Data Manager Agent follows to reconcile the schema.

The skill is invoked **on explicit trigger only** — never automatically on case load, never lazily on first query. This keeps the catalog's state deterministic and auditable.

# Steps

1. **Call `sync_catalog(case_id)`.** It invokes the reconciler and returns a typed diff with four parts: `auto_aliased`, `ambiguous`, `new`, `new_tables`. Entries in `auto_aliased` and `new` have already been persisted to the YAML profiles by the reconciler. Entries in `ambiguous` are NOT persisted — they need human input.

2. **For each entry in `auto_aliased`:** no action. The real column name has been appended to the canonical column's `aliases` list in the profile YAML. The reconciler is confident enough that no human confirmation is needed (either exact name match, known alias match, or normalized-name match with compatible dtype).

3. **For each entry in `ambiguous`:** present the real column to the human alongside the top-K candidate canonical columns, showing:
   - Real column name + inferred dtype + a few sample values
   - Each candidate's: canonical table, canonical column, fuzzy ratio, declared dtype, dtype-compatibility flag
   Ask the human to **pick a candidate**, **reject all** (treat as new), or **enter a different canonical column** (typo fix). On pick or typo-fix, append the real column to the chosen canonical's `aliases`. On reject, persist as a new column with `description_pending: true`.

4. **For each entry in `new`:** the reconciler has already written the column to the YAML with `description_pending: true`. If the column name matched a common-sense pattern (`*_id`, `*_date`, `*amount`, etc.), the reconciler pre-filled a provisional description. Otherwise the description is empty. In both cases the human must verify.

5. **Report the summary:** `"{N} auto-aliased, {M} ambiguous (awaiting human pick), {K} new ({J} drafted, {K-J} blank)"`.

# What this skill does NOT do

- **Does not verify descriptions.** Human verification happens out-of-band via `verify_description(table, col)` (optionally with an edited description text). The skill never flips `description_pending` to `false` on its own.
- **Does not touch row data.** Only table/column metadata is read; no CSV values are modified.
- **Does not re-run automatically.** One invocation per case, triggered explicitly.

# Notes on downstream behavior

Once sync has run, the catalog's case-filtered prompt context (`describe_catalog()` → `to_prompt_context(case_schema=...)`) renders each column with:
- Real column name (what agents query with)
- `[canonical: X]` annotation if the real name differs from the canonical
- `[parse: <format>]` if a `parse_hint` was inferred for string-stored dates
- `[UNVERIFIED]` marker if `description_pending: true`
- A banner at the top of the block if any column in the case is pending
```

- [ ] **Step 4: Run test to verify pass**

```bash
pytest tests/test_adapter.py::test_data_catalog_sync_skill_loads -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/workflow/data_catalog_sync.md tests/test_adapter.py
git commit -m "feat(skills): data_catalog_sync workflow skill for data_manager"
```

---

## Task 10: `DataManagerAgent.sync_catalog` + `verify_description`

**Files:**
- Modify: `agents/data_manager_agent.py`
- Create: `tests/test_catalog_sync.py`

- [ ] **Step 1: Write failing integration test**

Create `tests/test_catalog_sync.py`:

```python
"""End-to-end integration test for DataManagerAgent.sync_catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def sync_env(tmp_path):
    """Build a tmp case folder + profile directory mix, return a live agent."""
    # --- profile dir (seeded canonical catalog) ---
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "transactions.yaml").write_text("""\
table: transactions
description: "Transaction data"
columns:
  amount:
    dtype: float
    description: "Transaction amount"
    aliases: [trans_amt]
  transaction_date:
    dtype: date
    description: "Transaction date"
    aliases: []
""")

    # --- case folder (real CSVs) ---
    case_root = tmp_path / "data_tables"
    case_root.mkdir()
    case_dir = case_root / "case_A"
    case_dir.mkdir()
    (case_dir / "transactions.csv").write_text(
        "trans_amt,transaction_dt,mystery_field\n"
        "12.50,2025-01-01,foo\n"
        "30.00,2025-02-15,bar\n"
    )
    (case_dir / "brand_new_table.csv").write_text(
        "col_one,col_two\n1,a\n2,b\n"
    )

    # --- wire up real DataCatalog + SimulatedDataGateway + DataManagerAgent ---
    from data.catalog import DataCatalog
    from data.gateway import SimulatedDataGateway
    from agents.data_manager_agent import DataManagerAgent

    # Duck-typed stubs — avoid coupling the test to EventLogger/FirewalledModel
    # constructor signatures, which can evolve independently of this feature.
    class _NullLogger:
        def log(self, *args, **kwargs):
            pass

    class _NullLLM:
        pass

    catalog = DataCatalog(profile_dir=str(profile_dir))
    gateway = SimulatedDataGateway.from_case_folders(str(case_root))

    agent = DataManagerAgent(
        gateway=gateway,
        catalog=catalog,
        llm=_NullLLM(),
        logger=_NullLogger(),
    )
    return agent, profile_dir


def test_sync_catalog_end_to_end(sync_env):
    agent, profile_dir = sync_env
    diff = agent.sync_catalog("case_A")

    # auto: trans_amt is already an alias of amount.
    auto_cols = {e.real_col for e in diff.auto_aliased}
    assert "trans_amt" in auto_cols

    # ambiguous: transaction_dt should fuzzy-match transaction_date.
    ambig_cols = {e.real_col for e in diff.ambiguous}
    assert "transaction_dt" in ambig_cols

    # new: mystery_field has no match.
    new_cols = {e.real_col for e in diff.new}
    assert "mystery_field" in new_cols

    # brand_new_table flagged.
    assert "brand_new_table" in diff.new_tables

    # YAML persistence: new_col written with description_pending=true,
    # ambiguous NOT written into aliases.
    with open(profile_dir / "transactions.yaml") as f:
        trans = yaml.safe_load(f)
    assert "mystery_field" in trans["columns"]
    assert trans["columns"]["mystery_field"]["description_pending"] is True
    # Ambiguous not written:
    assert "transaction_dt" not in trans["columns"].get("transaction_date", {}).get("aliases", [])

    # brand_new_table.yaml was created:
    new_profile = profile_dir / "brand_new_table.yaml"
    assert new_profile.exists()
    with open(new_profile) as f:
        bnt = yaml.safe_load(f)
    assert "col_one" in bnt["columns"]
    assert bnt["columns"]["col_one"]["description_pending"] is True


def test_verify_description_flips_pending(sync_env):
    agent, profile_dir = sync_env
    agent.sync_catalog("case_A")

    # mystery_field was written with description_pending=true. Verify it.
    agent.verify_description(
        table="transactions",
        column="mystery_field",
        new_text="field of mysterious provenance",
    )

    with open(profile_dir / "transactions.yaml") as f:
        trans = yaml.safe_load(f)
    col = trans["columns"]["mystery_field"]
    assert col["description_pending"] is False
    assert col["description"] == "field of mysterious provenance"


def test_verify_description_without_edit(sync_env):
    """verify_description with no new_text just flips the flag."""
    agent, profile_dir = sync_env
    agent.sync_catalog("case_A")

    # Get the agent-drafted description first.
    with open(profile_dir / "transactions.yaml") as f:
        trans_before = yaml.safe_load(f)
    before_desc = trans_before["columns"]["mystery_field"]["description"]

    agent.verify_description(table="transactions", column="mystery_field")

    with open(profile_dir / "transactions.yaml") as f:
        trans_after = yaml.safe_load(f)
    col = trans_after["columns"]["mystery_field"]
    assert col["description_pending"] is False
    assert col["description"] == before_desc  # unchanged
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_catalog_sync.py -v
```

Expected: FAIL with `AttributeError: ... 'sync_catalog'`.

- [ ] **Step 3: Add `sync_catalog` and `verify_description` to DataManagerAgent**

Edit `agents/data_manager_agent.py`. First add imports near the top (alongside the existing `from data.catalog import DataCatalog`):

```python
from data import adapter
```

Add these methods to the `DataManagerAgent` class (place them after `describe_catalog`):

```python
    def sync_catalog(self, case_id: str) -> adapter.Diff:
        """Reconcile a real case folder against the canonical catalog.

        Auto-aliased matches and new columns are persisted to the YAML
        profiles. Ambiguous matches are returned but NOT persisted —
        callers (typically the data_catalog_sync skill) must resolve them
        with human input and then either update the catalog via
        `catalog.write_profile_patch` or accept them as new entries.
        """
        self.logger.log("data_manager_sync_start", {"case_id": case_id})
        canonical = {
            table: self.catalog._profiles[table]["columns"]
            for table in self.catalog.list_tables()
        }
        diff = adapter.reconcile_case(self.gateway, canonical, case_id)
        adapter.apply_diff(diff, self.catalog)
        self.logger.log(
            "data_manager_sync_done",
            {
                "case_id": case_id,
                "auto": len(diff.auto_aliased),
                "ambiguous": len(diff.ambiguous),
                "new": len(diff.new),
                "new_tables": len(diff.new_tables),
            },
        )
        return diff

    def verify_description(
        self,
        table: str,
        column: str,
        new_text: str | None = None,
    ) -> None:
        """Mark a column's description as human-verified.

        If `new_text` is provided, the description is overwritten with it.
        `description_pending` is flipped to False in both cases.
        """
        patch: dict = {"columns": {column: {"description_pending": False}}}
        if new_text is not None:
            patch["columns"][column]["description"] = new_text
        self.catalog.write_profile_patch(table, patch)
        self.logger.log(
            "data_manager_verify_desc",
            {"table": table, "column": column, "edited": new_text is not None},
        )
```

- [ ] **Step 4: Update `describe_catalog` to pass case schema into catalog**

Replace the existing `describe_catalog` method with:

```python
    def describe_catalog(self) -> str:
        """Return the catalog prompt-context filtered to the current case,
        preceded by the data_catalog skill body.

        When no case is active, falls back to the full (unfiltered) catalog
        so the Orchestrator can still see the overall shape at pre-case time.
        """
        if self.catalog is None:
            return self._catalog_prompt

        case_schema = self._build_case_schema()
        context = self.catalog.to_prompt_context(case_schema=case_schema)
        return f"{self._catalog_prompt}\n\n{context}".rstrip()

    def _build_case_schema(self) -> dict[str, list[str]] | None:
        """Return {table: [real_col_names]} for the current case, or None if
        no case is set (falls back to full-catalog rendering).
        """
        if self.gateway.get_case_id() is None:
            return None
        schema: dict[str, list[str]] = {}
        for table in self.gateway.list_tables():
            rows = self.gateway.query(table) or []
            schema[table] = list(rows[0].keys()) if rows else []
        return schema
```

- [ ] **Step 5: Run integration tests**

```bash
pytest tests/test_catalog_sync.py -v
```

Expected: all 3 tests PASS.

Then run the full adapter suite to confirm nothing regressed:

```bash
pytest tests/test_adapter.py tests/test_catalog_sync.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add agents/data_manager_agent.py tests/test_catalog_sync.py
git commit -m "feat(data_manager): sync_catalog + verify_description + case-filtered describe"
```

---

## Task 11: Full-repo regression sweep

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

```bash
pytest tests/ -v --tb=short
```

Expected: no failures. If any pre-existing catalog-dependent test fails due to the `to_prompt_context` signature change (it should be backwards compatible — no-arg call still works), investigate and fix.

- [ ] **Step 2: Confirm pandas-scope guard still holds**

```bash
pytest tests/test_adapter.py::test_pandas_scope -v
```

Expected: PASS (no `import pandas` leaked into gateway, catalog, agents, or tools).

- [ ] **Step 3: Smoke-test the system-under-test's main entry point**

If the repo's `main.py` or equivalent driver runs an end-to-end case analysis:

```bash
python -c "from agents.data_manager_agent import DataManagerAgent; print('ok')"
```

Expected: prints `ok`. If there's a main driver script, run it in dry-run mode with an existing case to confirm `describe_catalog()` still produces valid output (no exceptions).

- [ ] **Step 4: Final commit if anything changed**

If any fix was needed in Step 1:

```bash
git add <files>
git commit -m "fix: <description of the specific regression resolved>"
```

Otherwise no commit needed for this task.

---

## Known Issues / Deferred

**YAML comment preservation.** The existing `config/data_profiles/*.yaml` files contain human tuning comments (e.g., `# TUNE: average FICO score for cases under review`). `yaml.safe_dump` does not preserve comments — the first `write_profile_patch` call on an existing profile will strip all comments from that file. If comment preservation is required, swap `PyYAML` for `ruamel.yaml` inside `data/catalog.py` (scoped only to `write_profile_patch`; reads can stay on `PyYAML`). This is a one-line dependency addition but adds a second YAML library to the codebase. **Recommendation for MVP:** accept comment loss; document in the spec's Open Questions section if it becomes painful. Users can always recover prior comments from git.

**Ambiguous entries have no persistence.** Currently, ambiguous matches are returned in the diff but never stored — if the operator doesn't resolve them immediately, the next `sync_catalog` call will re-surface them from scratch. For MVP this is fine (sync is explicit, usually followed by resolution in the same session). If operators need to defer resolution, add an `ambiguous_pending` list in a catalog-meta YAML.

**Concurrent sync is unguarded.** Two concurrent `sync_catalog` calls on different cases can race on YAML writes. MVP assumes single-threaded, human-in-loop usage. Add an `fcntl.flock` on each profile file in `write_profile_patch` if that assumption breaks.
