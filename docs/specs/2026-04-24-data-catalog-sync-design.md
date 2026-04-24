# Data Catalog Sync — Design Spec

**Date:** 2026-04-24
**Depends on:** existing `DataCatalog` (`data/catalog.py`) and `SimulatedDataGateway` (`data/gateway.py`); existing YAML profiles in `config/data_profiles/*.yaml`; existing skill-loader pattern in `skills/workflow/*.md`.

## Goal

Add a schema-adapter layer and a reconciliation skill so that when a real case folder (`data_tables/<case>/*.csv`) has column names, table names, or dtypes that diverge from the shared canonical catalog, the system can:

1. Detect the divergences with a lightweight, deterministic matcher,
2. Auto-resolve confident matches (recorded as aliases on the canonical column),
3. Surface ambiguous matches to a human for a one-click pick, and
4. Capture genuinely new tables/columns for human description — with optional agent-drafted descriptions marked `description_pending: true` until verified.

The catalog becomes the single source of truth for "what data is available in this case, and what does each column mean." The shared catalog is global; each case renders a filtered view over it.

## Non-goals

- Not replacing `SimulatedDataGateway` with a real DB backend — that's the `data_agent_future_vision` work.
- Not adding per-case observed-dtype tracking (different cases storing the same concept differently) — MVP assumes one canonical dtype per column, with optional `parse_hint` for string-stored types.
- Not adding per-source alias scoping (Approach C in brainstorm) — aliases are global.
- Not gating queries on `description_pending` — this is a soft warn, not a hard block.
- Not adding pandas to the query hot path — pandas is scoped to `data/adapter.py` (sync-time only).

## §1 — Architecture & file layout

```
data/
  gateway.py           (existing)  SimulatedDataGateway — unchanged
  catalog.py           (existing)  DataCatalog — gains to_prompt_context(case_id),
                                   write_profile_patch(table, patch), render_column(row)
  adapter.py           (NEW)       pure reconciliation logic; imports pandas
config/
  data_profiles/
    *.yaml             (existing)  extended with aliases, parse_hint, description_pending
skills/
  workflow/
    data_catalog_sync.md  (NEW)    reconciliation skill for data-manager agent
agents/
  data_manager_agent.py  (existing) gains sync_catalog(case_id) + verify_description(table, col)
tests/
  test_adapter.py         (NEW)
  test_catalog_sync.py    (NEW)
requirements.txt          (modified)  + pandas
```

### Query-time data flow (unchanged — no adapter in the hot path)

```
CSV rows → SimulatedDataGateway.from_case_folders → rows keyed by REAL names
         → list_tables() / query(table, filter_column=real_name) → consumer agents
         (catalog provides real→canonical mapping + dtype/description for reference)
```

Rows are returned with the **real CSV column names**. No renames happen. Agents see and query with the names that physically exist in the case's CSVs, annotated by the catalog with their canonical concept.

### Sync-time data flow (new, explicit trigger only)

```
user or orchestrator → data_manager.sync_catalog(case_id)
                     → adapter.reconcile_case(gateway, catalog, case_id)
                         (per real column: 4-stage match against canonical aliases)
                     → diff report { auto_aliased, ambiguous, new }
                     → human-review prompt for ambiguous/new
                     → YAML patches written to config/data_profiles/*.yaml
```

Sync never runs automatically. Triggered explicitly by user, by a CLI command, or by the orchestrator as a deliberate step — not on boot, not lazily on first query.

## §2 — Recognition algorithm

Pure functions in `data/adapter.py`, stdlib + pandas only. For each real column in the case's CSVs, the matcher runs a four-stage cascade against every canonical column + its known aliases:

1. **Stage 1 — Exact.** `real_name == canonical_name OR real_name ∈ canonical.aliases` → **auto-match**.
2. **Stage 2 — Normalized.** `normalize(real_name) == normalize(any_canonical_name_or_alias)`, where `normalize` = lowercase, strip non-alphanumeric, trim trailing version digits → **auto-match** unless dtype is *clearly incompatible* (see below), in which case → **ambiguous**.
3. **Stage 3 — Fuzzy.** `difflib.SequenceMatcher` ratio on normalized forms; ratio ≥ `FUZZY_THRESHOLD` (default **0.85**) → candidate. Collect top-K (default **3**). All stage-3 hits → **ambiguous**. Ratio ranks candidates.
4. **Stage 4 — Dtype as signal, not filter.** For stages 2 and 3, dtype is shown alongside the ratio for human review but never rejects a candidate on its own. "Clearly incompatible" for stage 2 means sample values cannot plausibly be parsed to the canonical type (uses pandas: `pd.to_numeric(..., errors='coerce')`, `pd.to_datetime(..., errors='coerce')` — parse success rate < 50% flips to ambiguous).

Output buckets per case:
- **`auto_aliased`** — stages 1–2 with compatible dtype; real name is appended to `canonical.aliases` in the profile YAML.
- **`ambiguous`** — stage 2 with dtype mismatch, OR stage 3 candidates; top-K shown to human with ratio + dtype signal.
- **`new`** — zero candidates at any stage; column is genuinely unknown. Entry is created in the profile YAML with `description_pending: true` and (optionally) an agent-drafted description for obvious patterns.

Thresholds are module-level constants at the top of `adapter.py` — tunable without a config file.

```python
# data/adapter.py
FUZZY_THRESHOLD = 0.85      # minimum ratio to surface as ambiguous candidate
TOP_K = 3                   # max candidates shown per ambiguous column
DTYPE_COMPAT_THRESHOLD = 0.5  # min parse-success rate to call dtype "compatible"
```

## §3 — YAML catalog schema extensions

Three new optional fields per column (backwards compatible — existing 12 profiles need no migration):

```yaml
columns:
  amount:
    dtype: float
    distribution: normal
    mean: 50.0
    description: "dollar amount of the transaction"
    aliases: [trans_amt, txn_amount]   # NEW — real-world names observed across cases
    description_pending: false         # NEW — default false
    parse_hint: null                   # NEW — optional strptime pattern if stored as string
```

For newly-discovered columns that sync creates:

```yaml
some_new_col:
  dtype: float                         # inferred by adapter from CSV samples
  description: "proposed — agent's guess, unverified"   # or empty string if no obvious pattern
  aliases: [some_new_col]              # the real name observed
  description_pending: true            # blocks nothing; surfaces as [UNVERIFIED] to consumers
  parse_hint: null
```

Missing fields default to `[]` / `false` / `null`. No migration needed.

**Single description field, not two.** An agent-drafted description is written directly to `description` with `description_pending: true`. Human verification is a one-line edit (optionally editing the text first): flip `description_pending: false`. Audit trail lives in git.

**`parse_hint` use.** If present, the query layer calls `datetime.strptime(value, parse_hint)` before date comparisons. Only columns that need it carry the hint (e.g., `trans_dt` stored as `"Nov'2025"` gets `parse_hint: "%b'%Y"`). Sync auto-detects the hint by trying a small set of common patterns and picking the one with the highest parse-success rate (pandas `to_datetime(errors='coerce')`).

## §4 — Reconciliation skill workflow

New skill file following the existing `skills/workflow/*.md` convention (Markdown body + YAML frontmatter):

```yaml
---
name: data_catalog_sync
description: Reconcile a real case folder's schema against the shared data catalog
type: workflow
owner: data_manager_agent
mode: inline
tools: [sync_catalog, verify_description]
---
```

The body instructs the data-manager agent to:

1. Call `sync_catalog(case_id)` — returns a typed diff report.
2. For each entry in `auto_aliased`: no action needed (already persisted to YAML by `reconcile_case`).
3. For each entry in `ambiguous`: present the real column + top-K candidates + ratio + dtype signal to the human, wait for pick / reject / "actually new". On pick, append to the chosen canonical's `aliases`. On reject, fall through to "new".
4. For each entry in `new`: if the column name matches a common-sense pattern (`*_id`, `*_date`, `amount`, `balance`, `count`, `rate`, `score`), draft a short description and write. Otherwise write with `description: ""`. Always `description_pending: true`.
5. Report summary: `"{N} auto-aliased, {M} ambiguous awaiting pick, {K} new ({J} drafted, {K-J} blank)"`.
6. **Does NOT** flip `description_pending: false`. Human verification is out-of-band via `data_manager.verify_description(table, col)` (optionally with an edited description text).

The skill body is the agent's decision procedure. The mutation of YAML files happens inside `adapter.reconcile_case` and `catalog.write_profile_patch` — not in the skill itself.

### New agent methods

```python
# agents/data_manager_agent.py additions
def sync_catalog(self, case_id: str) -> dict:
    """Run the reconciler against the named case. Returns diff report."""

def verify_description(self, table: str, column: str, new_text: str | None = None) -> None:
    """Human-triggered: flip description_pending to false, optionally editing description first."""
```

## §5 — Downstream consumer behavior

### Catalog rendering: per-case view

`DataCatalog.to_prompt_context(case_id: str)` gains a case-filtered mode:

- Only tables that are physically present in this case's CSV folder are rendered.
- Per column: `real_name (dtype) [canonical: X] — "<description>"` — plus `[UNVERIFIED]` marker if `description_pending: true`. The `[canonical: X]` annotation is omitted when `real_name == canonical_name` (common case — no need to repeat the same string).
- Header banner if any column in the case is pending: `"⚠ Some columns in this case have unverified descriptions — treat them cautiously."`

Example:

```
Table: transactions
⚠ Some columns in this case have unverified descriptions — treat them cautiously.
Columns:
  trans_amt  (float)   [canonical: amount]           — "dollar amount of the transaction"
  trans_dt   (string)  [canonical: transaction_date] — "date the transaction posted" [parse: %b'%Y]
  merchant   (string)                                — "merchant name — unverified draft" [UNVERIFIED]
```

### Query path unchanged

`query(table, filter_column=real_name)` behaves exactly as today. No gating on `description_pending`. The warning is informational, embedded in the catalog's prompt context, not enforced programmatically.

### No per-specialist system-prompt amendment

The per-column `[UNVERIFIED]` marker and header banner in the catalog context are the only signals. We do **not** add additional "footnote unverified columns" instructions to specialist system prompts — the catalog-level signal is sufficient for MVP.

## §6 — New dependency: pandas (scoped)

Added to `requirements.txt`:

```
pandas>=2.0.0,<3.0.0
```

**Scope boundary:** pandas is imported only in `data/adapter.py`. It is NOT imported by `gateway.py`, `catalog.py`, `agents/*`, or `tools/*`. Enforced by convention + a small test (`test_adapter.py::test_pandas_scope`) that greps the source tree.

**Rationale:** reliable dtype inference and date-format detection on messy real-world CSVs is the matcher's core job. Hand-rolled parsers accumulate edge-case bugs; pandas does this in ~10 lines. Pandas runs only at sync time (explicit trigger, rare), not in the query hot path. numpy is already installed; pandas adds ~40MB install and one-time ~0.5s import — acceptable for the accuracy gain.

## §7 — Testing approach

### `tests/test_adapter.py` — unit

- `normalize_name`: case folding, punctuation stripping, trailing-digit trim, idempotence.
- `match_column`: each of the four stages produces the expected bucket.
- Threshold edges: ratios 0.849, 0.851, and hypothetical auto-match edges.
- Dtype compatibility: pure-numeric samples against a float canonical → compatible; "Nov'2025"-style samples against a date canonical with `pd.to_datetime(errors='coerce')` → compatible; mixed alpha against int canonical → incompatible.
- Deterministic ordering for ambiguous output (ratio desc, then canonical name asc).
- `test_pandas_scope`: grep `data/gateway.py`, `data/catalog.py`, `agents/*`, `tools/*` for `import pandas` — must find zero.

### `tests/test_catalog_sync.py` — integration (no mocks)

- Fixture: `tmp_path / "data_tables" / "case_001" / *.csv` — CSVs containing a deliberate mix: known-alias columns, fuzzy-match candidates, and brand-new columns.
- Fixture: `tmp_path / "config" / "data_profiles" / *.yaml` — seeded profiles with known aliases.
- Invoke `data_manager.sync_catalog("case_001")` and assert:
  - Auto-aliased YAML patches correctly append to canonical `aliases`.
  - Ambiguous entries are returned in the diff but NOT written to YAML.
  - New entries are written with `description_pending: true`.
  - Existing non-pending entries are untouched.
  - `parse_hint` is correctly inferred for a fixture date-as-string column.
- Uses real `SimulatedDataGateway.from_case_folders(tmp_path / "data_tables")` — no gateway mocking, matching existing repo test conventions.

## §8 — Open questions / deferred

- **Per-case observed dtype.** If two cases store `amount` as `float` and `string` respectively, MVP requires one canonical dtype + per-case `parse_hint` override. If this becomes painful, revisit with per-case metadata files.
- **Per-source alias scoping.** If real feeds from different source systems adopt conflicting names for the same concept, MVP will hit a conflict. Mitigation then: introduce `sources/` alias scoping (Approach C from brainstorm).
- **Alias deprecation.** No path currently for removing an alias when it's wrong. Manual YAML edit works for MVP.
- **Concurrent sync.** Two concurrent `sync_catalog` calls could race on YAML writes. MVP assumes sync is single-threaded (explicit trigger, human in loop). Add a file lock if that assumption breaks.
