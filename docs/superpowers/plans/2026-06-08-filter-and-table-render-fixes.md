# Filter Robustness + Table-Render Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop filters from returning false "no records" on value/format mismatch (Workstream C), and make the `make_chart(kind="table")` transaction-row artifact actually render (Workstream D, backend-only).

**Architecture:** All filter logic lives in `tools/data_tools.py` (`_apply_filter` → `_coerce_pair` → operator; date parsing in `_date_key`; column resolution in `_resolve_real_column`). The table-render fix is one validation guard in `tools/data_viz_tools.py` plus skill-doc wording. The frontend (`../CaseReviewChat`) already renders `kind="table"` and needs no changes (verified).

**Tech Stack:** Python 3, pytest. Reference spec: `docs/superpowers/specs/2026-06-08-filter-and-table-render-fixes-design.md`.

**Branch:** `feat/filter-table-fixes` (already checked out).

**Baseline:** `pytest -q` currently reports **7 failed, 414 passed**. The 7 failures are pre-existing and unrelated (`tests/test_server.py` prune/warmth helpers; `tests/test_datalayer/test_generator.py::test_profile_names` missing `txn_monthly` concept). Do NOT attribute them to this work; the bar is "no NEW failures" + new tests pass.

**Conventions:** working dir path contains spaces — always quote it: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"`. Follow TDD: write the failing test, see it fail, implement, see it pass, commit.

---

## File Structure

**Modify:**
- `tools/data_tools.py` — C1–C6: `_apply_filter`, `_coerce_pair` (+ new `_is_strict_number`), `_resolve_real_column`, `_date_key` (+ new regexes/imports).
- `tools/data_viz_tools.py` — D1: relax `y_fields` requirement for `kind="table"`.
- `skills/workflow/data_query.md` — C2 (`contains` mention) + D3 (un-suppress table path).
- `skills/workflow/data_viz.md` — D2 (table example + column contract).
- `.claude/CLAUDE.md` — C6 date-list upkeep + correction.

**Tests (extend existing files):**
- `tests/test_tools/test_data_tools.py` — C1–C5 + C6 date cases.
- `tests/test_tools/test_data_viz_tools.py` — D1.
- `tests/test_server.py` — D4 (verify/extend the existing table-emission test near line 382).

---

## Task 1: C2 — add `contains` operator + C1/C4 string-eq & null-ne fixes in `_apply_filter`

These three changes all live in the same function (`_apply_filter`), so they ship together.

**Files:**
- Modify: `tools/data_tools.py` (`_apply_filter`, ~589–628)
- Test: `tests/test_tools/test_data_tools.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tools/test_data_tools.py` (this file already sets up `data_tools` via an autouse fixture with `LocalDataGateway`/`DataCatalog`; these tests call `_apply_filter` directly, which needs no case context):

```python
from tools.data_tools import _apply_filter


def _rows():
    return [
        {"merchant": "WALMART", "amt": 10, "flag": None},
        {"merchant": " Walmart ", "amt": 20, "flag": "x"},
        {"merchant": "Target", "amt": 30, "flag": None},
    ]


def test_eq_is_case_and_whitespace_insensitive_for_text():
    out = _apply_filter(_rows(), "merchant", "walmart", "eq")
    assert len(out) == 2  # "WALMART" and " Walmart "


def test_ne_excludes_case_insensitive_matches_and_counts_nulls():
    out = _apply_filter(_rows(), "merchant", "walmart", "ne")
    # Only "Target" differs by value; null cells also satisfy ne.
    merchants = sorted(str(r["merchant"]) for r in out)
    assert merchants == ["Target"]


def test_ne_counts_null_cells():
    out = _apply_filter(_rows(), "flag", "x", "ne")
    # the row with flag=="x" is excluded; the 2 null-flag rows satisfy ne
    assert len(out) == 2


def test_contains_matches_substring_case_insensitively():
    rows = [{"m": "STARBUCKS #4412 SEATTLE WA"}, {"m": "WALMART"}]
    out = _apply_filter(rows, "m", "starbucks", "contains")
    assert len(out) == 1 and out[0]["m"].startswith("STARBUCKS")


def test_numeric_eq_still_exact_after_string_changes():
    rows = [{"code": 0}, {"code": 1}, {"code": 1}]
    assert len(_apply_filter(rows, "code", "1", "eq")) == 2
    assert len(_apply_filter(rows, "code", "0", "eq")) == 1
```

- [ ] **Step 2: Run, verify failure**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "contains or case_and_whitespace or counts_null or ne_excludes" -v
```
Expected: FAILs (`contains` returns all rows / case-sensitive eq misses, null-ne under-counts).

- [ ] **Step 3: Implement**

In `tools/data_tools.py`, replace the `_apply_filter` body from the `between` block through the end (current lines ~600–628) with:

```python
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
```

Also update the docstring line in `_apply_filter` from `Supported ops: eq, ne, gt, gte, lt, lte, between.` to `Supported ops: eq, ne, gt, gte, lt, lte, between, contains.`

- [ ] **Step 4: Run, verify pass**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "contains or case_and_whitespace or counts_null or ne_excludes or numeric_eq_still_exact" -v
```
Expected: all PASS.

- [ ] **Step 5: Check for an op-whitelist elsewhere**

Some tool impls may validate `filter_op` against a fixed set before calling `_apply_filter`. Confirm `contains` isn't rejected upstream:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
grep -n "gte\", \"lt\|valid_ops\|allowed.*op\|filter_op not in\|{\"eq\"" tools/data_tools.py
```
If you find a whitelist that lists eq/ne/gt/… but not `contains`/`between`, add `"contains"` (and `"between"` if missing) to it. If none exists, no action.

- [ ] **Step 6: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add tools/data_tools.py tests/test_tools/test_data_tools.py
git commit -m "fix(filter): case-insensitive eq/ne, add contains op, count nulls for ne"
```

---

## Task 2: C3 — gate numeric coercion to preserve ID/code columns

**Files:**
- Modify: `tools/data_tools.py` (`_coerce_pair` ~312–330, + new `_is_strict_number` helper)
- Test: `tests/test_tools/test_data_tools.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
from tools.data_tools import _coerce_pair


def test_leading_zero_ids_stay_strings():
    a, b = _coerce_pair("007", "7")
    assert a != b  # not coerced to 7.0 == 7.0


def test_plain_numbers_still_coerce():
    assert _coerce_pair(0, "0") == (0.0, 0.0)
    assert _coerce_pair("0.7", 0.7) == (0.7, 0.7)
    assert _coerce_pair("188800", "188800") == (188800.0, 188800.0)


def test_scientific_and_inf_nan_not_numeric():
    a, b = _coerce_pair("1e3", "1000")
    assert a != b  # "1e3" stays a string, not 1000.0
```

- [ ] **Step 2: Run, verify failure**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "leading_zero or plain_numbers or scientific_and_inf" -v
```
Expected: `test_leading_zero_ids_stay_strings` and `test_scientific_and_inf_nan_not_numeric` FAIL (current `float()` coerces both).

- [ ] **Step 3: Implement**

In `tools/data_tools.py`, add this helper immediately ABOVE `def _coerce_pair` (after the date code, ~line 311):

```python
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
```

Then replace the numeric branch of `_coerce_pair` (current lines ~319–323):

```python
    # 1) numeric
    try:
        return float(a), float(b)
    except (TypeError, ValueError):
        pass
```

with:

```python
    # 1) numeric — only when BOTH sides are strictly numeric, so ID/code
    #    columns ("007", zip codes, "1e3") and inf/nan don't get coerced.
    if _is_strict_number(a) and _is_strict_number(b):
        return float(a), float(b)
```

- [ ] **Step 4: Run, verify pass**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "leading_zero or plain_numbers or scientific_and_inf or numeric_eq_still_exact" -v
```
Expected: all PASS. Then run the whole filter-related set to confirm no regressions:
```bash
pytest tests/test_tools/test_data_tools.py -v
```
Expected: no NEW failures vs baseline (this file should be fully green).

- [ ] **Step 5: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add tools/data_tools.py tests/test_tools/test_data_tools.py
git commit -m "fix(filter): gate numeric coercion so leading-zero IDs stay strings"
```

---

## Task 3: C5 — refuse ambiguous fuzzy column resolution

**Files:**
- Modify: `tools/data_tools.py` (`_resolve_real_column` fuzzy stage ~580–586)
- Test: `tests/test_tools/test_data_tools.py`

- [ ] **Step 1: Write failing test**

Append:

```python
from tools.data_tools import _resolve_real_column


def test_fuzzy_column_refuses_ambiguous_match():
    rows = [{"score_1": 1, "score_2": 2}]
    # "score_1" exists exactly → returns it.
    assert _resolve_real_column(rows, "score_1", None) == "score_1"
    # "score_9" doesn't exist; "score_1" and "score_2" both normalize to
    # "score" → ambiguous → refuse, return the literal (honest miss).
    assert _resolve_real_column(rows, "score_9", None) == "score_9"


def test_fuzzy_column_unique_match_still_resolves():
    rows = [{"Merchant Risk Score": 0.5}]
    assert _resolve_real_column(rows, "merchant_risk_score", None) == "Merchant Risk Score"
```

- [ ] **Step 2: Run, verify failure**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "fuzzy_column" -v
```
Expected: `test_fuzzy_column_refuses_ambiguous_match` FAILS (currently `score_9` resolves to `score_1`, the first normalized match).

Note: `score_1`/`score_2` normalize to `score` because `_normalize` strips trailing digits. `score_9` also normalizes to `score`, matching both → that's the ambiguity we now refuse.

- [ ] **Step 3: Implement**

Replace the fuzzy fallback in `_resolve_real_column` (current lines ~580–586):

```python
    target = _normalize(requested)
    if not target:
        return requested
    for k in real_keys:
        if _normalize(k) == target:
            return k
    return requested
```

with:

```python
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
```

- [ ] **Step 4: Run, verify pass**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "fuzzy_column" -v
```
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add tools/data_tools.py tests/test_tools/test_data_tools.py
git commit -m "fix(filter): refuse ambiguous fuzzy column match instead of silent mis-bind"
```

---

## Task 4: C6 — date-format coverage (MMM-YY, YYYY-MMM, Excel serial; confirm MMM-YYYY & tz)

**Files:**
- Modify: `tools/data_tools.py` (imports, new regexes near ~145, branches in `_date_key`)
- Test: `tests/test_tools/test_data_tools.py`

Note from source inspection (do not re-add these — already covered):
- `MMM-YYYY` ("Jul-2025"): `_MONTH_YEAR_RE` already matches 3-letter month + 4-digit year, and `_MONTHS` has 3-letter keys → already parses to `(2025, 7, 1)`.
- TZ-aware ISO datetime ("2024-01-01 15:25:20.602+00:00", "...Z"): `_ISO_DATETIME_RE`'s `[\d:.+\-Z]+` already accepts the offset/Z forms.
We still add CONFIRMING tests for those two so the coverage is locked.

- [ ] **Step 1: Write failing/locking tests**

Append:

```python
from tools.data_tools import _date_key


def test_date_key_mmm_yy():
    assert _date_key("Jul-25") == (2025, 7, 1)
    assert _date_key("Jul'25") == (2025, 7, 1)


def test_date_key_year_mmm():
    assert _date_key("2025-Jul") == (2025, 7, 1)


def test_date_key_excel_serial():
    # 45292 = 2024-01-01 (Excel epoch 1899-12-30)
    assert _date_key("45292") == (2024, 1, 1)


def test_date_key_mmm_yyyy_already_covered():
    assert _date_key("Jul-2025") == (2025, 7, 1)


def test_date_key_tz_aware_datetime_already_covered():
    assert _date_key("2024-01-01 15:25:20.602+00:00") == (2024, 1, 1)
    assert _date_key("2024-01-01T15:25:20Z") == (2024, 1, 1)
```

Also add the equivalents into the existing parametrized
`test_summarize_trend_handles_extended_date_formats` in this file (find it and add `("Jul-25", ...)`, `("2025-Jul", ...)`, `("45292", ...)` rows following the established param tuple shape) — per the CLAUDE.md date-handling rule that every new `_date_key` format gets a `summarize_trend` param case.

- [ ] **Step 2: Run, verify failure**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "date_key_mmm_yy or date_key_year_mmm or date_key_excel" -v
```
Expected: these FAIL (`Jul-25`, `2025-Jul`, `45292` → currently None). The `_already_covered` tests should already PASS.

- [ ] **Step 3: Implement**

In `tools/data_tools.py`:

(a) Ensure the date imports exist at the top of the file (add if missing — check first with `grep -n "from datetime import\|^import datetime" tools/data_tools.py`):
```python
from datetime import date, timedelta
```

(b) Add these regexes next to the others (after `_MONTH_YEAR_RE`, ~line 145):
```python
# Month + 2-digit year: "Jul-25", "Jul'25", "July 25".
_MONTH_2YEAR_RE = re.compile(r"^([A-Za-z]{3,})\s*[-'\s]\s*(\d{2})$")
# Year + month name: "2025-Jul", "2025 July".
_YEAR_MONTH_NAME_RE = re.compile(r"^(\d{4})\s*[-'\s]\s*([A-Za-z]{3,})$")
# Excel serial date base (serial 1 = 1900-01-01; Excel's 1900 leap bug means
# the usable epoch offset is 1899-12-30).
_EXCEL_EPOCH = date(1899, 12, 30)
```

(c) Add the `MMM-YY` and `YYYY-MMM` branches in `_date_key` immediately AFTER the existing `_MONTH_YEAR_RE` block (after current line ~292):
```python
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
```

(d) Add the Excel-serial branch just BEFORE the final `return None` (after the `_YEAR_RE` block, ~line 307):
```python
    # Excel serial date: a bare 5-digit integer in the plausible range
    # (~1954..2064). Narrow range so ordinary 5-digit counts aren't misread.
    if s.isdigit() and len(s) == 5:
        serial = int(s)
        if 20000 <= serial <= 60000:
            d = _EXCEL_EPOCH + timedelta(days=serial)
            return (d.year, d.month, d.day)
```

- [ ] **Step 4: Run, verify pass**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_tools.py -k "date_key or extended_date_formats" -v
```
Expected: all PASS (new + already-covered + the parametrized set).

- [ ] **Step 5: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add tools/data_tools.py tests/test_tools/test_data_tools.py
git commit -m "fix(dates): parse MMM-YY, YYYY-MMM, Excel serials; lock MMM-YYYY/tz coverage"
```

---

## Task 5: C6 doc upkeep + C2 skill mention

**Files:**
- Modify: `.claude/CLAUDE.md` (date list)
- Modify: `skills/workflow/data_query.md` (`contains` in the filter-rigor section)

- [ ] **Step 1: Update `.claude/CLAUDE.md`**

In the "Date / time format handling is LOAD-BEARING" section: move `MMM-YY`, `MMM-YYYY`, `YYYY-MMM`, `pandas timestamps with timezone`, and `Excel serial numbers` out of the "Common gaps to be ready for" line and into the "Already covered (do not regress)" list. Also correct the inaccurate sentence (it claims a `2024-01` filter won't `eq`-match a datetime cell) — replace with: "Note: `_date_key` collapses datetimes to day-grain, so a `2024-01-01` filter DOES match a `2024-01-01 15:25:20` cell." Read the file first to place these edits precisely.

- [ ] **Step 2: Update `skills/workflow/data_query.md`**

In the "Filter rigor" section added earlier, update the free-text bullet to name the operator. Read the file, find the bullet starting "For **free-text entity columns**", and ensure it reads:
> - For **free-text entity columns** (merchant name, reason codes), prefer the **`contains`** operator (case-insensitive substring) over exact `eq`. `eq`/`ne` are now case- and whitespace-insensitive for text, so case alone won't cause a miss — but `contains` is the right tool when the stored value has extra tokens (e.g. "STARBUCKS #4412 SEATTLE WA").

- [ ] **Step 3: Verify skills still load**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_skills/ -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add .claude/CLAUDE.md skills/workflow/data_query.md
git commit -m "docs: record new date formats as covered; document contains operator"
```

---

## Task 6: D1 — allow `kind="table"` with empty `y_fields`

**Files:**
- Modify: `tools/data_viz_tools.py` (validation ~159–165; KP build ~221–229; table return msg ~249)
- Test: `tests/test_tools/test_data_viz_tools.py`

- [ ] **Step 1: Write failing test**

Read the existing async table test for the fixture/harness pattern:
```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
sed -n '554,640p' tests/test_tools/test_data_viz_tools.py
```
Then add a test modeled on `test_make_chart_table_kind_skips_render_and_persists_kp` but passing `y_fields=[]`:

```python
@pytest.mark.asyncio
async def test_make_chart_table_kind_accepts_empty_y_fields(tmp_path):
    """A table with no explicit y_fields is valid — the frontend derives
    columns from the row keys. Must NOT return a `[make_chart error]`."""
    # ... build the same ctx/kb/case_folder harness the sibling table test uses ...
    out = await make_chart(
        ctx,
        topic="march_declines",
        kind="table",
        claim="Two declined transactions in March.",
        points=[{"date": "2024-03-04", "amount": 120.5, "decision": "declined"}],
        x_field="date",
        y_fields=[],
        source_call="query_table('model_scores_transaction', ...)",
    )
    assert "[make_chart error]" not in out
    # KP persisted with the table viz + numbers
    kp = kb["<specialist_name>"][-1]   # match how the sibling test reads kb
    assert kp["viz"]["kind"] == "table"
    assert kp["numbers"]
```

Mirror the exact harness (ctx object, `kb` wiring, specialist name) from the sibling test you just read — do not invent a new fixture.

- [ ] **Step 2: Run, verify failure**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_viz_tools.py -k "empty_y_fields" -v
```
Expected: FAIL — returns `[make_chart error] y_fields must be a non-empty list...`.

- [ ] **Step 3: Implement**

In `tools/data_viz_tools.py`, change the `y_fields` validation (current line ~159):

```python
        if not isinstance(y_fields, list) or not y_fields:
```
to:
```python
        if kind != "table" and (not isinstance(y_fields, list) or not y_fields):
```

Make the KP build defensive against a non-list/empty `y_fields` (current line ~225):
```python
            "viz": {"kind": kind, "x_field": x_field, "y_fields": list(y_fields)},
```
to:
```python
            "viz": {
                "kind": kind,
                "x_field": x_field,
                "y_fields": list(y_fields) if isinstance(y_fields, list) else [],
            },
```

In the table-kind success return (current line ~249, the `({len(points)} rows × {len(y_fields)} columns)` text), compute the column count from the row when `y_fields` is empty so the message isn't "0 columns":
```python
        n_cols = len(y_fields) if y_fields else len(points[0]) if points else 0
```
and use `n_cols` in that return string instead of `len(y_fields)`. (Read lines ~236–255 to place this cleanly within the `if kind == "table":` block.)

- [ ] **Step 4: Run, verify pass + no regression**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_tools/test_data_viz_tools.py -v
```
Expected: new test PASS; all existing viz tests still PASS (esp. `test_make_chart_table_kind_skips_render_and_persists_kp`, `test_make_chart_table_kind_accepts_one_row`, and the plot-kind y_fields-required tests — plot kinds must STILL reject empty y_fields).

- [ ] **Step 5: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add tools/data_viz_tools.py tests/test_tools/test_data_viz_tools.py
git commit -m "fix(viz): allow make_chart kind=table without y_fields (frontend derives columns)"
```

---

## Task 7: D2 + D3 — document table contract & un-suppress the path

**Files:**
- Modify: `skills/workflow/data_viz.md` (table row + example)
- Modify: `skills/workflow/data_query.md` (un-suppress for transaction rows)

- [ ] **Step 1: Update `skills/workflow/data_viz.md`**

Read the file. In the "Pick the kind" table, change the `table` row's `y_fields shape` cell from `any` to `optional — empty = show all keys`. Then add a worked example after the existing multi-series example block (near line ~60):

```markdown
**Table (show specific rows):**
```
points=[{"date":"2024-03-04","amount":120.50,"decision":"declined","reason":"R12"},
        {"date":"2024-03-09","amount":80.00,"decision":"approved","reason":""}]
make_chart(topic="march_transactions", kind="table",
           x_field="date", y_fields=["amount","decision","reason"],
           claim="Two March transactions; one declined (R12).", source_call="query_table(...)")
# kind="table": x_field = the row-label column (optional);
#   y_fields = value columns to show, in order (optional — empty shows all keys).
#   No minimum row count; no image rendered — the rows surface as a table card.
```
```

- [ ] **Step 2: Update `skills/workflow/data_query.md`**

Read the file. Two edits:

(a) In the "you do NOT need to call `make_chart`" guidance (~46–52), append a sentence so the table path isn't suppressed:
> Exception: when the answer IS a set of specific transactions/rows the reviewer should see, DO call `make_chart(kind="table", ...)` with those rows — the auto-renderer only produces trend/bar charts, never row tables.

(b) Update the "Show the transactions in transaction-level answers" section (added in the parent PR, ~line 200–206) — replace the "planned" wording with the now-working instruction:
> For transaction-level answers, surface the specific transactions both ways: (1) a compact **markdown table** in your evidence (always works in the answer), and (2) a **`make_chart(kind="table")`** call with those rows so they render as an interactive table card in the Plots panel.

- [ ] **Step 3: Verify skills load**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_skills/ -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add skills/workflow/data_viz.md skills/workflow/data_query.md
git commit -m "docs(skills): document kind=table contract and instruct using it for txn rows"
```

---

## Task 8: D4 — backend emission test for `kind="table"`

**Files:**
- Modify/extend: `tests/test_server.py` (existing table-related test near line ~382)

- [ ] **Step 1: Inspect the existing table-emission test**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
sed -n '360,440p' tests/test_server.py
grep -n "_collect_turn_charts\|def _find_kp\|kind.*table\|numbers" tests/test_server.py | head
```

- [ ] **Step 2: Decide — verify or add**

If an existing test already asserts that a `kind="table"` KP in the specialist KB produces a `chart` payload carrying `kind=="table"` + `numbers` (and no required `url`), then D4 is already covered — just run it and note that in your report; no new test needed.

If NOT, add a test (following the file's existing harness) that:
1. Builds a `specialist_kb` dict containing one table KP: `{"<spec>": [{"topic": "t", "claim": "c", "numbers": [{"a": 1}], "viz": {"kind": "table", "x_field": "a", "y_fields": []}, "captured_at_turn": "<turn>"}]}`.
2. Calls `_collect_turn_charts(kb, turn_id, case_id)` and the payload-building path used at server.py ~1701–1731 (mirror how the existing chart tests exercise it).
3. Asserts the resulting payload has `kind == "table"`, a populated `numbers`, and does not require a non-empty `url`.

Use the exact import + harness style already present in `tests/test_server.py`.

- [ ] **Step 3: Run**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest tests/test_server.py -k "table or chart" -v
```
Expected: the table-emission assertion PASSES (the 7 pre-existing `test_server.py` failures are in prune/warmth tests, unrelated — ignore those).

- [ ] **Step 4: Commit**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
git add tests/test_server.py
git commit -m "test(server): assert kind=table KP emits a chart payload with numbers"
```

---

## Task 9: Full regression + acceptance

- [ ] **Step 1: Full suite**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
pytest -q 2>&1 | tail -5
```
Expected: failure count is **≤ 7** and every failure is one of the known pre-existing ones (prune/warmth in `test_server.py`, `test_profile_names` in `test_generator.py`). The passed count should be baseline 414 + all the new tests. If ANY new failure appears (especially in `test_data_tools.py`, `test_data_viz_tools.py`, `test_skills/`), investigate before proceeding.

- [ ] **Step 2: End-to-end mismatch acceptance**

```bash
cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2"
python3 -c "
from tools.data_tools import _apply_filter, _date_key, _coerce_pair
# case-insensitive eq + contains
rows=[{'m':'WALMART'},{'m':'walmart inc'},{'m':'Target'}]
print('eq walmart ->', len(_apply_filter(rows,'m','walmart','eq')))      # 1
print('contains walmart ->', len(_apply_filter(rows,'m','walmart','contains')))  # 2
print('id 007!=7 ->', _coerce_pair('007','7'))
print('Jul-25 ->', _date_key('Jul-25'))
print('45292 ->', _date_key('45292'))
"
```
Expected: `eq walmart -> 1`, `contains walmart -> 2`, `id 007!=7 -> ('007', '7')`, `Jul-25 -> (2025, 7, 1)`, `45292 -> (2024, 1, 1)`.

- [ ] **Step 3: Spec coverage check**

Re-read `docs/superpowers/specs/2026-06-08-filter-and-table-render-fixes-design.md` §3–4 and confirm each item (C1–C7, D1–D4) maps to a completed task. Confirm frontend was correctly left untouched.

- [ ] **Step 4: Do NOT push**

Report completion; ask the user whether to push + open the PR.

---

## Self-Review (plan author)

- **Spec coverage:** C1+C4 → Task 1; C2 → Task 1 (+docs Task 5); C3 → Task 2; C5 → Task 3; C6 → Task 4 (+doc Task 5); C7 (interaction) → Task 9 Step 2 + the `numeric_eq_still_exact` test in Task 1. D1 → Task 6; D2 → Task 7; D3 → Task 7; D4 → Task 8; frontend "no change" → asserted in Task 9 Step 3. All covered.
- **Placeholder scan:** Tasks 6 and 8 intentionally instruct the implementer to mirror an EXISTING test harness (ctx/kb wiring) rather than reproducing a fixture the plan can't see verbatim — the implementer reads the sibling test first. All code edits to `data_tools.py`/`data_viz_tools.py` are given as exact before/after blocks. One inline note flags a non-ASCII char (`除外`) to replace if present.
- **Type/name consistency:** `_apply_filter`, `_coerce_pair`, `_is_strict_number`, `_resolve_real_column`, `_date_key`, `_STRICT_NUMBER_RE`, `_MONTH_2YEAR_RE`, `_YEAR_MONTH_NAME_RE`, `_EXCEL_EPOCH` used consistently. `contains`/`between` operator handling matches `_apply_filter`'s structure.
- **Risk:** C1 (forgiving eq) is the highest-impact change; Task 1's `test_numeric_eq_still_exact` and Task 9's acceptance guard the numeric/date paths. C3's regex is covered by an explicit allow/reject test set.
```
