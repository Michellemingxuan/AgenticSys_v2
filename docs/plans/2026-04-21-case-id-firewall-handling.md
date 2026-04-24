# Case-ID Firewall Handling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent case-ID tokens (`CASE-\d+`) from reaching LLM-bound content. Clean up the data-profile layer so `case_id` is generator infrastructure, not table schema; add a defense-in-depth scrubber at the LLM boundary.

**Architecture:** Four surgical changes (profile cleanup, tool output wording, gateway error paths, catalog cleanup) plus one new module (`gateway/case_scrubber.py`) integrated into `SafeChainAdapter`. Matches the existing two-layer firewall strategy.

**Tech Stack:** Python 3 · pytest · PyYAML · numpy (generator).

**Spec:** [docs/specs/2026-04-21-case-id-firewall-handling-design.md](../specs/2026-04-21-case-id-firewall-handling-design.md)

---

## File Structure

**New files:**
- `config/generation.yaml` — single source of truth for generation parameters (`n_cases`)
- `gateway/case_scrubber.py` — case-ID token masking module
- `tests/test_gateway/test_case_scrubber.py` — unit tests for the scrubber

**Modified files:**
- `data/generator.py` — add `CASE_ID_COLUMN`, `CASE_ID_FORMAT`; inject `case_id` column; load `generation.yaml`
- `data/__main__.py` — read `n_cases` default from `generation.yaml`
- `data/catalog.py` — remove dead `if col == "case_id": continue` filter
- `data/gateway.py` — add `_display_path()` helper; use in error strings; docstring warning on `get_case_id()`
- `tools/data_tools.py` — reword `list_available_tables()` output
- `gateway/safechain_adapter.py` — call `case_scrubber.scrub()` during pre-sanitize
- `config/data_profiles/*.yaml` (all 11) — remove `case_id` column block
- `tests/test_tools/test_data_tools.py` — update assertions for new wording and schema
- `tests/test_data/test_generator.py` — assert generator still emits `case_id` with YAMLs that no longer declare it

---

## Task 1: Generator injects `case_id` as infrastructure

**Files:**
- Modify: `data/generator.py`
- Test: `tests/test_data/test_generator.py`

This task adds generator-side `case_id` injection using module-level constants. The injection is **idempotent**: if a profile still declares `case_id`, the generator leaves the existing column alone. After Task 2 removes `case_id` from every YAML, injection becomes the sole source. This ordering lets tests pass at every commit.

- [ ] **Step 1: Write the failing test for injection**

Add to `tests/test_data/test_generator.py`:

```python
def test_generator_injects_case_id_column(tmp_path):
    """Generator adds a case_id column to every table, even when the YAML profile does not declare it."""
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "noop.yaml").write_text(
        "table: noop\n"
        "description: minimal fixture with no case_id column\n"
        "one_row_per_case: true\n"
        "columns:\n"
        "  value:\n"
        "    dtype: int\n"
        "    distribution: uniform\n"
        "    min: 0\n"
        "    max: 10\n"
        "    description: placeholder\n"
    )

    from data.generator import DataGenerator, CASE_ID_COLUMN, CASE_ID_FORMAT
    gen = DataGenerator(profile_dir=str(profile_dir), seed=1, cases=3)
    gen.load_profiles()
    tables = gen.generate_all()

    cols = tables["noop"]
    assert CASE_ID_COLUMN in cols
    # 3 cases, one_row_per_case → 3 rows with CASE-00001..CASE-00003
    assert cols[CASE_ID_COLUMN] == [
        CASE_ID_FORMAT.format(seq=1),
        CASE_ID_FORMAT.format(seq=2),
        CASE_ID_FORMAT.format(seq=3),
    ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_data/test_generator.py::test_generator_injects_case_id_column -v`
Expected: FAIL — `ImportError: cannot import name 'CASE_ID_COLUMN' from 'data.generator'`

- [ ] **Step 3: Add constants and injection**

Edit `data/generator.py`. Add module-level constants near the top (after imports, before `class DataGenerator`):

```python
CASE_ID_COLUMN = "case_id"
CASE_ID_FORMAT = "CASE-{seq:05d}"
```

Then in `_generate_table()`, after the derived-column loop (after line 82 `columns[col_name] = self._derive_column(spec, columns, n)`), add:

```python
        # Inject case_id column as generator infrastructure (idempotent — skip if profile still declares it).
        if CASE_ID_COLUMN not in columns:
            one_row = profile.get("one_row_per_case", False)
            case_count = self._get_case_count()
            if one_row:
                columns[CASE_ID_COLUMN] = [CASE_ID_FORMAT.format(seq=i + 1) for i in range(n)]
            else:
                columns[CASE_ID_COLUMN] = [CASE_ID_FORMAT.format(seq=(i % case_count) + 1) for i in range(n)]
```

- [ ] **Step 4: Run the new test + full generator suite**

Run: `pytest tests/test_data/test_generator.py -v`
Expected: all pass (new test passes; existing tests unchanged since real profiles still declare `case_id`, so the idempotent branch is taken).

- [ ] **Step 5: Commit**

```bash
git add data/generator.py tests/test_data/test_generator.py
git commit -m "feat(generator): inject case_id column as infrastructure

Add CASE_ID_COLUMN and CASE_ID_FORMAT constants. Generator now injects
case_id idempotently — skips if a profile still declares it, adds it
otherwise. Prepares for removing case_id from per-table YAMLs."
```

---

## Task 2: Remove `case_id` from all 11 data profiles

**Files:**
- Modify: `config/data_profiles/bureau.yaml`, `cross_bu.yaml`, `cust_tenure.yaml`, `income_dti.yaml`, `model_scores.yaml`, `payments.yaml`, `score_drivers.yaml`, `spends.yaml`, `txn_monthly.yaml`, `wcc_flags.yaml`, `xbu_summary.yaml`
- Test: `tests/test_data/test_generator.py` (existing + new regression assertion)

- [ ] **Step 1: Write a failing regression test**

Add to `tests/test_data/test_generator.py`:

```python
def test_generator_full_suite_no_profile_case_id():
    """All 11 real profiles generate correctly with case_id removed from YAMLs.

    After Task 2, no profile declares case_id. The generator should still produce
    a case_id column in every table (via the infrastructure injection from Task 1).
    """
    from data.generator import DataGenerator, CASE_ID_COLUMN

    gen = DataGenerator(profile_dir="config/data_profiles", seed=42, cases=5)
    gen.load_profiles()

    # Sanity: no profile declares case_id anymore
    for table_name, profile in gen.profiles.items():
        assert CASE_ID_COLUMN not in profile["columns"], (
            f"{table_name}.yaml still declares case_id — remove it"
        )

    tables = gen.generate_all()
    # Every generated table has case_id with the right format
    import re
    pattern = re.compile(r"^CASE-\d{5}$")
    for table_name, cols in tables.items():
        assert CASE_ID_COLUMN in cols, f"{table_name} missing case_id column"
        for v in cols[CASE_ID_COLUMN]:
            assert pattern.match(v), f"{table_name} has bad case_id: {v!r}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_data/test_generator.py::test_generator_full_suite_no_profile_case_id -v`
Expected: FAIL — assertion `case_id not in profile["columns"]` fires for `bureau.yaml` first.

- [ ] **Step 3: Remove the `case_id:` block from each of the 11 YAMLs**

Each profile has a block like:

```yaml
  case_id:
    dtype: string
    format: "CASE-{seq:05d}"
    description: "..."
```

Delete the entire block (usually 4 lines) from each of:
- `config/data_profiles/bureau.yaml`
- `config/data_profiles/cross_bu.yaml`
- `config/data_profiles/cust_tenure.yaml`
- `config/data_profiles/income_dti.yaml`
- `config/data_profiles/model_scores.yaml`
- `config/data_profiles/payments.yaml`
- `config/data_profiles/score_drivers.yaml`
- `config/data_profiles/spends.yaml`
- `config/data_profiles/txn_monthly.yaml`
- `config/data_profiles/wcc_flags.yaml`
- `config/data_profiles/xbu_summary.yaml`

Verify each YAML still has a `columns:` key with the remaining columns and that indentation is intact.

- [ ] **Step 4: Run full generator suite**

Run: `pytest tests/test_data/test_generator.py -v`
Expected: all pass (including the new regression test).

- [ ] **Step 5: Regenerate the sample data to confirm end-to-end**

Run: `python -m data --output data/simulated/ --seed 42 --cases 5`
Expected: prints `5 cases, 55 files total` (11 tables × 5 cases) with no tracebacks. Spot-check `data/simulated/CASE-00001/payments.csv` — header should NOT contain `case_id`.

- [ ] **Step 6: Commit**

```bash
git add config/data_profiles/ tests/test_data/test_generator.py
git commit -m "refactor(profiles): remove case_id from all 11 data profiles

case_id is now injected by the generator as infrastructure. Per-table
YAMLs describe real data columns only. Eliminates the upstream leak
where catalog.get_schema() advertised case_id to the LLM."
```

---

## Task 3: Introduce `config/generation.yaml` as single source of truth

**Files:**
- Create: `config/generation.yaml`
- Modify: `data/__main__.py`
- Test: `tests/test_data/test_generator.py` (optional — CLI not currently tested)

- [ ] **Step 1: Create the generation config**

Create `config/generation.yaml`:

```yaml
# Single source of truth for data-generation parameters.
# Overridden by CLI flags in `python -m data`.
n_cases: 50
```

- [ ] **Step 2: Wire into `data/__main__.py`**

Replace the section in `data/__main__.py` from line 34 onwards:

```python
    if args.cases and args.row_count:
        parser.error("--cases and --row-count are mutually exclusive")

    cases = args.cases or _load_default_n_cases()
```

And add at module scope (after imports):

```python
import yaml
from pathlib import Path

_GENERATION_CONFIG = Path("config/generation.yaml")
_FALLBACK_N_CASES = 50


def _load_default_n_cases() -> int:
    """Read n_cases from config/generation.yaml, fall back to 50 if missing."""
    if not _GENERATION_CONFIG.exists():
        return _FALLBACK_N_CASES
    with open(_GENERATION_CONFIG) as f:
        cfg = yaml.safe_load(f) or {}
    return int(cfg.get("n_cases", _FALLBACK_N_CASES))
```

Remove the hard-coded `50` literal from the existing `cases = args.cases or 50` line.

- [ ] **Step 3: Run the generator end-to-end**

Run: `python -m data --output /tmp/gen_test --seed 42`
Expected: `Generating 50 cases` printed (picked up from `config/generation.yaml`). Cleanup: `rm -rf /tmp/gen_test`.

- [ ] **Step 4: Run: `python -m data --output /tmp/gen_test --seed 42 --cases 7`**

Expected: `Generating 7 cases` printed (CLI override works). Cleanup.

- [ ] **Step 5: Commit**

```bash
git add config/generation.yaml data/__main__.py
git commit -m "feat(generator): read n_cases default from config/generation.yaml

CLI --cases flag still overrides. Removes implicit coupling where
case_id's row count determined the generation size."
```

---

## Task 4: Remove the dead `case_id` filter in catalog

**Files:**
- Modify: `data/catalog.py`
- Modify: `tests/test_data/test_gateway.py` — update `test_catalog_get_schema` (it asserts `"case_id" in schema`, which is now false after Task 2)
- Test: `tests/test_data/test_generator.py` (add `test_catalog_prompt_context_has_no_case_id`)

Once Task 2 lands, the defensive filter at `data/catalog.py:94-95` is dead code. Removing it prevents the catalog from silently hiding a column it no longer knows about. Task 2 also broke an existing catalog test assertion — fix it here as part of the same "catalog knows case_id is infrastructure" surface.

- [ ] **Step 1: Write a failing test asserting `to_prompt_context()` omits `case_id` naturally**

Add to `tests/test_data/test_generator.py` (or a dedicated `tests/test_data/test_catalog.py` if you prefer — the one-line addition below is fine):

```python
def test_catalog_prompt_context_has_no_case_id():
    from data.catalog import DataCatalog
    cat = DataCatalog(profile_dir="config/data_profiles")
    ctx = cat.to_prompt_context()
    # No profile declares case_id anymore; catalog must not mention it either.
    assert "case_id" not in ctx
    assert "CASE-" not in ctx
```

- [ ] **Step 2: Run to verify it passes already (profile cleanup from Task 2 does the work)**

Run: `pytest tests/test_data/test_generator.py::test_catalog_prompt_context_has_no_case_id -v`
Expected: PASS (nothing to do). If it fails, a profile in Task 2 was missed — fix.

- [ ] **Step 3: Remove the dead filter**

Edit `data/catalog.py`. Delete lines 94-95 (the two lines of the `if col == "case_id":` block):

```python
                for col, info in details.items():
                    if col == "case_id":              # DELETE
                        continue  # case_id is implicit from context    # DELETE
                    col_desc = info.get("description", "")
```

After deletion the loop body starts directly at `col_desc = info.get(...)`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_data/ -v`
Expected: all pass. The `to_prompt_context` test still passes; removing the filter didn't regress because no profile declares `case_id` anymore.

- [ ] **Step 5: Commit**

```bash
git add data/catalog.py tests/test_data/test_generator.py
git commit -m "refactor(catalog): remove dead case_id filter in to_prompt_context

After the profile cleanup in the previous commit, no profile declares
case_id, so the defensive filter is no longer reachable. Removing it
prevents the catalog from silently hiding a column it no longer knows
about."
```

---

## Task 5: Reword `list_available_tables()` to hide the case ID

**Files:**
- Modify: `tools/data_tools.py`
- Test: `tests/test_tools/test_data_tools.py`

- [ ] **Step 1: Update the failing existing test + add a leak-check test**

Edit `tests/test_tools/test_data_tools.py`. Replace the existing `test_list_tables` (around line 35):

```python
def test_list_tables():
    result = data_tools.list_available_tables()
    assert "bureau_full" in result
    assert "Tables for the current case:" in result
    # No raw case ID must leak.
    import re
    assert re.search(r"CASE-\d+", result) is None
```

Also update `test_get_schema` (around line 41) — after Task 2, `case_id` is not in the schema anymore:

```python
def test_get_schema():
    result = data_tools.get_table_schema("bureau")
    assert "type" in result
    # case_id is infrastructure, not schema — must not appear in LLM-bound schema output.
    assert "case_id" not in result
    assert "CASE-" not in result
```

- [ ] **Step 2: Run to verify the updated tests fail**

Run: `pytest tests/test_tools/test_data_tools.py::test_list_tables tests/test_tools/test_data_tools.py::test_get_schema -v`
Expected: `test_list_tables` FAILS (regex finds `CASE-00001` in current output). `test_get_schema` — may PASS already if Task 2 landed (schema no longer contains `case_id`); if FAIL, check Task 2.

- [ ] **Step 3: Apply the wording change in `tools/data_tools.py`**

Edit `tools/data_tools.py` lines 27-39. Replace the `list_available_tables` body with:

```python
def list_available_tables() -> str:
    """List all data tables available for the current case."""
    if _catalog is None:
        return "Data unavailable"
    if _gateway is not None:
        case_tables = _gateway.list_tables()
        if case_tables:
            return "Tables for the current case:\n" + "\n".join(case_tables)
        return "No tables available for the current case."
    tables = _catalog.list_tables()
    return "\n".join(tables) if tables else "No tables available"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_tools/test_data_tools.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/data_tools.py tests/test_tools/test_data_tools.py
git commit -m "fix(tools): hide case ID in list_available_tables output

LLM now sees 'Tables for the current case:' instead of the raw case ID.
Specialists already reason in terms of the current case; the identifier
itself was never needed in tool output and risked firewall flagging."
```

---

## Task 6: Gateway — `_display_path()` helper + docstring warning

**Files:**
- Modify: `data/gateway.py`
- Test: `tests/test_data/test_gateway.py`

- [ ] **Step 1: Write a failing test**

Add to `tests/test_data/test_gateway.py`:

```python
def test_gateway_error_uses_neutral_path_token():
    """Error strings surfaced to callers must use <case> token, not the raw case ID."""
    from data.gateway import SimulatedDataGateway

    gw = SimulatedDataGateway(case_data={"CASE-00001": {"payments": [{"amt": 100}]}})
    gw.set_case("CASE-00001")

    # _display_path returns the neutral form regardless of the current case.
    assert gw._display_path("payments") == "<case>/payments.csv"
    assert "CASE-" not in gw._display_path("payments")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_data/test_gateway.py::test_gateway_error_uses_neutral_path_token -v`
Expected: FAIL — `AttributeError: 'SimulatedDataGateway' object has no attribute '_display_path'`.

- [ ] **Step 3: Add `_display_path()` to the abstract base class**

Edit `data/gateway.py`. In the `DataGateway` abstract class (around line 19), add:

```python
    def _display_path(self, table: str) -> str:
        """Render a path for user/LLM-facing messages without leaking the raw case ID.

        Real filesystem paths stay internal; any string that can flow back to a caller,
        tool result, or LLM prompt should use this helper instead.
        """
        return f"<case>/{table}.csv"
```

- [ ] **Step 4: Add the docstring warning on `get_case_id()`**

Edit the existing `get_case_id` method (around line 27 in the abstract, and its concrete override):

```python
    def get_case_id(self) -> str | None:
        """Return the currently active case_id.

        WARNING: The return value MUST NOT be included in any LLM-bound string
        (tool result, prompt, error message). Use `_display_path()` or the
        '<case>' literal when composing LLM-bound content.
        """
```

- [ ] **Step 5: Audit and update existing error strings**

Search for any error/exception string in `data/gateway.py` that could include the current case ID or a filesystem path:

```bash
grep -n 'f"' data/gateway.py
```

For each match, if the f-string embeds a real path or the case ID AND can be returned to a caller (e.g., returned from `query()` or raised), rewrite using `self._display_path(table)`. Example pattern:

```python
# before
raise FileNotFoundError(f"table not found: {path}")
# after
raise FileNotFoundError(f"table not found at {self._display_path(table)}")
```

If no such strings exist, note it in the commit message.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_data/test_gateway.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add data/gateway.py tests/test_data/test_gateway.py
git commit -m "feat(gateway): neutral <case> token for LLM-bound path strings

Add _display_path() helper and a docstring warning on get_case_id().
Internal callers keep the real path; anything flowing to LLM context
uses the <case> token."
```

---

## Task 7: Create `gateway/case_scrubber.py`

**Files:**
- Create: `gateway/case_scrubber.py`
- Create: `tests/test_gateway/test_case_scrubber.py`

- [ ] **Step 1: Write failing unit tests**

Create `tests/test_gateway/test_case_scrubber.py`:

```python
"""Unit tests for gateway.case_scrubber."""

import pytest

from gateway.case_scrubber import scrub


def test_scrub_basic_token():
    assert scrub("see CASE-00001 payments") == "see <case> payments"


def test_scrub_case_insensitive():
    assert scrub("see case-00001") == "see <case>"
    assert scrub("see Case-42") == "see <case>"


def test_scrub_multiple_tokens():
    result = scrub("CASE-00001 and CASE-00002")
    assert result == "<case> and <case>"


def test_scrub_embedded_in_json():
    import json
    payload = json.dumps({"ref": "CASE-00007", "other": "fine"})
    scrubbed = scrub(payload)
    assert "CASE-00007" not in scrubbed
    assert "<case>" in scrubbed


def test_scrub_idempotent():
    once = scrub("CASE-00001")
    twice = scrub(once)
    assert once == twice == "<case>"


def test_scrub_empty_and_no_match():
    assert scrub("") == ""
    assert scrub("no case-ish content here") == "no case-ish content here"


def test_scrub_respects_word_boundaries():
    # A string that merely contains the substring "CASE-" as part of a larger token
    # should be scrubbed if followed by digits (that's the whole point), but a bare
    # "CASE-" with no digits should NOT be touched.
    assert scrub("CASE-notanumber") == "CASE-notanumber"
    assert scrub("CASE-") == "CASE-"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_gateway/test_case_scrubber.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.case_scrubber'`.

- [ ] **Step 3: Create the module**

Create `gateway/case_scrubber.py`:

```python
"""Mask case-ID tokens (CASE-\\d+) before content flows to the LLM.

Used by SafeChainAdapter as a defense-in-depth layer: even if upstream
leaks (a new tool, an error string, a specialist's own output) contain
a raw case ID, the boundary scrubber masks it before the prompt reaches
the model.

Scope is intentionally narrow — one rule, nothing else. Digit masking,
role-label neutralization, and exec-keyword filtering live elsewhere in
SafeChainAdapter/FirewallStack.
"""

from __future__ import annotations

import re

_CASE_TOKEN = re.compile(r"\bCASE-\d+\b", flags=re.IGNORECASE)


def scrub(text: str) -> str:
    """Replace CASE-\\d+ tokens (case-insensitive) with the literal '<case>'.

    Idempotent: scrub(scrub(x)) == scrub(x) for all strings.
    """
    return _CASE_TOKEN.sub("<case>", text)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_gateway/test_case_scrubber.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add gateway/case_scrubber.py tests/test_gateway/test_case_scrubber.py
git commit -m "feat(gateway): add case_scrubber module for CASE- token masking

Single-responsibility module used by SafeChainAdapter at the LLM
boundary. Idempotent, case-insensitive, word-boundary-aware."
```

---

## Task 8: Wire scrubber into `SafeChainAdapter._invoke`

**Files:**
- Modify: `gateway/safechain_adapter.py`
- Test: add to `tests/test_gateway/` (new `test_safechain_adapter.py` if none exists — check first)

- [ ] **Step 1: Check for an existing adapter test file**

Run: `ls tests/test_gateway/`
If `test_safechain_adapter.py` exists, extend it. If not, create it.

- [ ] **Step 2: Write failing tests that target the new static method**

Create or extend `tests/test_gateway/test_safechain_adapter.py`:

```python
"""Tests for SafeChainAdapter pre-sanitization."""

from gateway.safechain_adapter import SafeChainAdapter


def test_pre_sanitize_masks_case_token():
    sample = "Context:\nTables for CASE-00001.\n\nRequest:\nAnalyze payments."
    cleaned = SafeChainAdapter._pre_sanitize(sample)
    assert "CASE-00001" not in cleaned
    assert "<case>" in cleaned


def test_pre_sanitize_masks_long_digit_runs():
    cleaned = SafeChainAdapter._pre_sanitize("account 12345678901234")
    assert "12345678901234" not in cleaned
    assert "***MASKED***" in cleaned


def test_pre_sanitize_filters_exec_keywords():
    cleaned = SafeChainAdapter._pre_sanitize("please exec this")
    assert "[FILTERED]" in cleaned


def test_pre_sanitize_preserves_benign_text():
    sample = "Nothing sensitive, just a short note."
    assert SafeChainAdapter._pre_sanitize(sample) == sample
```

These tests call a method that does not yet exist — refactoring the inline sanitizer into a static method both enables testing and makes the scrubber ordering explicit.

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_gateway/test_safechain_adapter.py -v`
Expected: FAIL — `AttributeError: type object 'SafeChainAdapter' has no attribute '_pre_sanitize'`.

- [ ] **Step 4: Refactor `_invoke()` pre-sanitize into a static method + call scrubber**

Edit `gateway/safechain_adapter.py`. Replace lines 139-143 (the inline pre-sanitize block):

```python
        # Mask long digit sequences (potential account numbers / PII)
        combined = re.sub(r"\b\d{8,}\b", "***MASKED***", combined)

        # Strip code execution keywords from tool results
        combined = re.sub(r"\b(exec|eval|import|__\w+__)\b", "[FILTERED]", combined)
```

with a call to the new static method:

```python
        combined = self._pre_sanitize(combined)
```

Add the method on the class (placement: just above `_refresh_llm`):

```python
    @staticmethod
    def _pre_sanitize(text: str) -> str:
        """All defenses applied before the LLM sees the combined prompt.

        Order: case scrub → digit mask → exec keyword filter. Case scrubbing
        runs first because the digit mask could otherwise mangle a case-ID
        suffix (e.g., CASE-12345678 with an 8-digit run) before the case
        pattern matches.
        """
        from gateway.case_scrubber import scrub as case_scrub
        text = case_scrub(text)
        text = re.sub(r"\b\d{8,}\b", "***MASKED***", text)
        text = re.sub(r"\b(exec|eval|import|__\w+__)\b", "[FILTERED]", text)
        return text
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_gateway/test_safechain_adapter.py tests/test_gateway/test_case_scrubber.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add gateway/safechain_adapter.py tests/test_gateway/test_safechain_adapter.py
git commit -m "feat(safechain): scrub CASE- tokens in pre-sanitize pipeline

Extract the pre-sanitize block into SafeChainAdapter._pre_sanitize and
prepend case_scrubber.scrub() to the pipeline. Defense-in-depth guarantee
that case IDs cannot leak to the LLM even if upstream layers miss one."
```

---

## Task 9: Full-suite verification

**Files:** (read-only)

- [ ] **Step 1: Run the entire test suite**

Run: `pytest tests/ -v`
Expected: all pass. If anything unrelated fails, investigate and fix (no task should introduce regressions).

- [ ] **Step 2: Run a final grep for accidental leaks**

Run: `grep -rn "CASE-" --include="*.py" tools/ agents/ orchestrator/ data/`
Expected: no matches in `tools/`. Matches in `data/__main__.py` (CLI help text) and `data/gateway.py` (docstrings referencing the format) are acceptable — internal-only.

Run: `grep -rn 'case_id' config/data_profiles/`
Expected: no matches.

- [ ] **Step 3: Regenerate and spot-check**

Run: `python -m data --output /tmp/final_verify --seed 42 --cases 3 && head -1 /tmp/final_verify/CASE-00001/payments.csv`
Expected: header line with no `case_id` column. Cleanup: `rm -rf /tmp/final_verify`.

- [ ] **Step 4: Final commit (docs only — if spec needs any final tweak)**

If everything passes without changes, no commit needed. If the spec needs updating to reflect a detail discovered during implementation, commit the spec edit here.

---

## Done criteria

All of:
- [ ] Every task above committed.
- [ ] `pytest tests/` green.
- [ ] `grep -rn 'CASE-' tools/` returns zero matches.
- [ ] `grep -rn 'case_id' config/data_profiles/` returns zero matches.
- [ ] `python -m data --cases 3` produces per-case folders with no `case_id` column in the CSV headers.
