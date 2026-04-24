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
