# Filter robustness + table-render fixes — design (Workstreams C + D)

**Date:** 2026-06-08
**Status:** Design — awaiting spec review → implementation plan.
**Parent:** Follow-up to `docs/superpowers/specs/2026-06-08-transaction-level-data-design.md` (§5–6). That effort (profiles + instructions) shipped in PR #1; this spec covers the two deferred code-fix workstreams.

---

## 1. Problem & goal

Two correctness problems surfaced while adding transaction-level data, both confirmed by root-cause diagnosis:

- **C — Filters return zero records on value/format MISMATCH, not true absence.** The user: "current system may encounter mismatch sometimes and may output no records due to mismatch." Distinct causes: case/whitespace-sensitive `eq` on free-text; no substring/`contains` operator; over-eager numeric coercion corrupting ID/code columns; `ne` silently dropping nulls; fuzzy column-name resolution silently binding to the wrong column; several date formats unparseable.
- **D — `make_chart(kind="table")` "rarely works."** The interactive Plots-panel table almost never appears. Root cause is NOT the render pipeline (which works end-to-end, frontend included — see §4) but that the artifact is **never produced**: the skills tell specialists not to call `make_chart`, and the `kind="table"` argument contract is unclear/over-strict.

**Goal:** make filters forgiving of the mismatches that produce false "no data," and make the transaction-row table artifact actually render when a specialist wants to show rows.

**Design decisions (locked with the user):**
- C string matching: **`eq`/`ne` become case- and whitespace-insensitive by default for text values**, and add a case-insensitive **`contains`** operator. Numeric/date `eq` stay exact.
- C dates: add the **full CLAUDE.md "common gaps" set** (`MMM-YY`, `MMM-YYYY`, `YYYY-MMM`, timezone-aware pandas timestamps, Excel serial numbers).
- D: **fix the explicit `make_chart(kind="table")` path + instruct specialists to use it.** Do NOT add auto-distiller table emission.
- D frontend: **no frontend changes** — the renderer already exists and is verified working (§4).

---

## 2. Non-goals

- No auto-distiller `kind="table"` emission (`redacting_tool.py` unchanged for chart-kind logic).
- No frontend changes to `../CaseReviewChat` (renderer already complete — §4).
- No change to numeric/date `eq` semantics (only string `eq`/`ne` become forgiving).
- No new specialist/agent; no profile changes.

---

## 3. Workstream C — filter robustness (`tools/data_tools.py`)

All filtering for `query_table`, `aggregate_column`, `summarize_by_group`, and `summarize_trend`'s row-filter funnels through `_apply_filter` → `_coerce_pair` → operator. Date narrowing in `summarize_trend` uses `_date_key` directly. Anchors below are from the diagnosis; the plan re-verifies exact line numbers before editing.

> **Parity note:** these are `tools/data_tools.py` changes (the shared data layer), NOT `firewall_client.py`/`safechain_client.py`. The `feedback_openai_safechain_parity` rule does not apply here. Confirm no filter logic is duplicated in those clients before finishing.

### C1. Case/whitespace-insensitive `eq`/`ne` for text (default) — P0

- **Where:** `_coerce_pair` (~312–330) + `_apply_filter` (~589–628).
- **Behavior:** when a comparison falls to the **string** branch (value is neither numeric nor date on both sides) AND the operator is `eq` or `ne`, compare `str(a).strip().casefold()` vs `str(b).strip().casefold()`. So `eq "Walmart"` matches `"WALMART"`, `" walmart "`. `gt/gte/lt/lte` on strings keep current (raw) behavior (rare for text; lexical).
- **Implementation:** `_coerce_pair` is operator-agnostic, so do the normalization in `_apply_filter` for the eq/ne case: after `_coerce_pair`, if both coerced values are `str`, apply `.strip().casefold()` to both before the `operator.eq`/`operator.ne` call. Numeric/date coercions are unaffected (they short-circuit before the string branch).
- **Test:** `eq` matches across case and surrounding whitespace; `ne` excludes the case-insensitive match; numeric `eq` (`0`/`"0"`) and date `eq` still behave as today.

### C2. Add `contains` operator (case-insensitive substring) — P0

- **Where:** `_FILTER_OPS` (~52–59) + `_apply_filter` (~589–628).
- **Behavior:** `op="contains"` keeps rows where `str(filter_value).strip().casefold()` is a substring of `str(cell).casefold()`. Null cells do not match. Enables merchant-name-style entity filters ("starbucks" matches "STARBUCKS #4412 SEATTLE WA").
- **Implementation:** handle `contains` explicitly in `_apply_filter` before the `_FILTER_OPS` numeric/date path (it never coerces to number/date). Add `contains` to the documented operator list.
- **Docs:** add `contains` to the `filter_op` docstrings of `query_table`, `aggregate_column`, `summarize_by_group`, `summarize_trend`, `batch_*`, and mention it in `skills/workflow/data_query.md` filter-rigor section ("for free-text entity columns prefer `contains`").
- **Test:** `contains` matches a substring case-insensitively; returns the right `rows_matching_filter` count; empty result when genuinely absent.

### C3. Gate numeric coercion — preserve ID/code columns — P1

- **Where:** `_coerce_pair` (~319–323).
- **Problem:** `float()` is tried first and is too eager: `'007' == '7'`, `'02134'(zip) == '2134'`, `'1e3' == '1000'`, `'inf' == 'Infinity'`, `nan != nan`. Leading-zero account/zip/SIC filters match wrong rows.
- **Behavior:** only treat both sides as numeric when **both** match a strict decimal pattern that rejects leading-zero integers, scientific notation, and inf/nan: `^-?(0|[1-9]\d*)(\.\d+)?$` OR `^-?0\.\d+$` (allows `0`, `0.7`, `-5`, `188800`, `3.15`; rejects `007`, `0123`, `1e3`, `inf`, `nan`). Otherwise fall through to the date/string branches. This keeps `appr_deny_cd` `0`/`1` matching (`"0"`/`"1"` pass the pattern) while preserving leading-zero IDs as strings.
- **Test:** `0`/`"0"` and `0.7`/`"0.7"` still match numerically; `'007'` stays a string and does NOT equal `'7'`; `'1e3'` not equal `'1000'`.

### C4. `ne` must not drop null cells — P1

- **Where:** `_apply_filter` (~608–609, 621–623) — `if cell is None: continue` drops nulls for every op.
- **Behavior:** for `op="ne"`, a null cell **satisfies** `ne` (it is "not equal" to a concrete value) and should be counted. For eq/gt/gte/lt/lte/contains, dropping nulls stays correct.
- **Test:** a column with nulls filtered `ne <value>` counts the null rows; `eq` on the same column still excludes them.

### C5. Refuse ambiguous fuzzy column resolution — P2

- **Where:** `_normalize` (~65–70) + `_resolve_real_column` (~548–586, fuzzy fallback ~580–585).
- **Problem:** `_normalize` strips trailing digits and punctuation, so `score_1`/`score_2` → `score`, `bucket_30`/`bucket_60` → `bucket`. A requested-but-absent numbered column can silently bind to a sibling and filter the WRONG column → zero/wrong rows, no error.
- **Behavior:** when the normalized-fuzzy stage matches **2 or more** distinct real keys, **refuse** the fuzzy match and return the literal requested name (→ honest zero / clear "column not found" rather than silent-wrong). Exact-match and catalog-alias stages are unchanged. (Keep the existing `(resolved from 'x')` annotation when a unique fuzzy match IS made.)
- **Test:** requesting `score_1` when only `score_2` exists does NOT resolve to `score_2`; a unique fuzzy case (e.g. `Merchant Risk Score` → `merchant_risk_score`) still resolves.

### C6. Date-format coverage — full CLAUDE.md gap set — P3

- **Where:** `_date_key` (~184–309), `_MONTH_YEAR_RE` (~145).
- **Add branches (each normalized to a `(y, m, d)` tuple; day defaults to 1 for month-grain):**
  - `MMM-YY` — `Jul-25` → expand 2-digit year (pivot consistent with any existing 2-digit-year handling, e.g. 00–69 → 2000s, 70–99 → 1900s; reuse the existing `_expand_two_digit_year` helper if present).
  - `MMM-YYYY` — `Jul-2025` (3-letter month + 4-digit year), if not already covered by the `MonthName-YYYY` branch.
  - `YYYY-MMM` — `2025-Jul`.
  - **Timezone-aware pandas timestamps** — e.g. `2024-01-01 15:25:20.602+00:00` / `...Z` — strip the tz/offset suffix, then parse as the existing ISO-datetime branch (day-grain).
  - **Excel serial numbers** — a bare integer in the plausible serial range (~20000–60000) → days since the Excel epoch `1899-12-30`. Gate narrowly so real small integers aren't misread as dates (only apply in date-context parsing; `_date_key` already returns `None` for non-dates, so restrict the serial branch to a sane range).
- **Tests:** extend the existing parametrized `test_summarize_trend_handles_extended_date_formats` with one case per new format (per the CLAUDE.md date-handling rule), quoting representative sample values. Do not regress the already-covered formats.
- **Doc upkeep:** update the CLAUDE.md "Date / time format handling is LOAD-BEARING" list to move these from "common gaps to be ready for" to "already covered," and correct the inaccurate note that `2024-01` won't `eq`-match a datetime (it already does via day-collapse).

### C7 (interaction check)

After C1–C3, re-confirm the transaction acceptance from the parent spec still holds: `appr_deny_cd eq "1"` returns declines (numeric path), and a merchant/`contains` filter works. Add/keep a test in `tests/test_tools/`.

---

## 4. Workstream D — make `make_chart(kind="table")` actually render

### 4.1 Frontend status — VERIFIED COMPLETE, no changes

Confirmed by inspection of `../CaseReviewChat`:
- `src/hooks/useSSE.ts` (~244–273) `chart` handler reads `numbers`, `x_field`, `y_fields` into `ChartInfo`.
- `src/types.ts` (~92–126) `ChartInfo` includes `kind`, `numbers`, `x_field`, `y_fields`; `'table'` is a documented kind.
- `src/store.ts` `upsertChart` preserves those fields (dedup by specialist+topic).
- `src/components/PlotPanel/PlotPanel.tsx` render switch: `kind === 'table'` → `<DataTable>` (reads `numbers`, derives columns from `x_field`+`y_fields`, falls back to first-row keys, empty-state handling, `formatCell`). CSS in `PlotPanel.module.css`. 📋 icon.
- Markdown tables in the final `answer` render via `react-markdown` + `remark-gfm` (`MessageBubble.tsx`).

**Implication:** the moment the backend EMITS a `kind="table"` chart with a `numbers` payload, it renders. The whole problem is backend production. **No frontend task.**

### 4.2 Backend root cause (confirmed)

- The KP → `_collect_turn_charts` → `chart` SSE pipeline already handles table KPs (`server.py:576` keeps url-less table KPs; emission ~1701–1731 attaches `numbers`/`x_field`/`y_fields`). Not the bug.
- Tables are only ever produced by an explicit `make_chart(kind="table")` call, but:
  1. `skills/workflow/data_query.md` (~46–52, 152) tells specialists *"you do NOT need to call `make_chart`"* / *"charts render automatically"* — suppressing the only table path.
  2. `kind="table"` has no worked example and an unclear column contract (`skills/workflow/data_viz.md` ~31; `tools/data_viz_tools.py` docstring ~104 only says "table = 1-3 rows").
  3. `make_chart` validation (`tools/data_viz_tools.py` ~159–165) requires a non-empty `y_fields` even for tables, so a reasonable `y_fields=[]` ("just show these rows") errors out and the specialist gives up.

### D1. Allow `kind="table"` with empty/omitted `y_fields` — P0

- **Where:** `tools/data_viz_tools.py` validation (~141–253, esp. ~159–165).
- **Behavior:** for `kind="table"`, do NOT require `y_fields`. Contract: `x_field` = the row-label/key column (optional); `y_fields` = the value columns to display in order (optional). If both are omitted/empty, the stored KP simply carries `numbers` and the frontend derives columns from the first row's keys (already supported by `DataTable`). Keep requiring a non-empty `points`/`numbers` (a table with no rows is an error).
- **Test:** `make_chart(kind="table", points=[{...}], x_field="", y_fields=[])` stores a KP with `viz.kind=="table"` and `numbers` populated, and does not raise. (Extends existing `test_make_chart_table_kind_skips_render_and_persists_kp`.)

### D2. Document the `kind="table"` contract — P0

- **Where:** `tools/data_viz_tools.py` `make_chart` docstring (~104) + `skills/workflow/data_viz.md` (~31, add an example alongside the other kinds).
- **Content:** a worked example —
  ```
  points=[{"date":"2024-03-04","amount":120.50,"decision":"declined","reason":"R12"},
          {"date":"2024-03-09","amount":80.00,"decision":"approved","reason":""}]
  make_chart(topic="march_declines", kind="table",
             x_field="date", y_fields=["amount","decision","reason"], ...)
  # kind="table": x_field = row-label column (optional);
  #   y_fields = value columns to show in order (optional; empty = show all keys).
  ```

### D3. Un-suppress the table path for transaction-row answers — P0

- **Where:** `skills/workflow/data_query.md` — the "Show the transactions in transaction-level answers" section added in the parent PR, and the existing "you do NOT need to call `make_chart`" wording (~46–52).
- **Behavior:** keep the "charts auto-render for trends" guidance, but add an explicit carve-out: *"When the answer is a set of specific transactions/rows the reviewer should see, DO call `make_chart(kind='table', ...)` with those rows — the auto-renderer does not produce row tables."* Update the parent's interim wording (which said the Plots-panel table was "planned") to reflect that it now works.
- **Note:** the markdown-table-in-answer mechanism from the parent PR stays as the always-on fallback; D adds the richer Plots-panel artifact on top.

### D4. Backend emission test — P1

- **Where:** `tests/` (server/chart-collection level).
- **Behavior:** given a specialist KB containing a `kind="table"` KP (with `numbers`), `_collect_turn_charts` + the chart-emit path produce a `chart` payload with `kind=="table"`, `numbers`, `x_field`, `y_fields` and no required `url`. (Guards the contract the verified frontend depends on.)

---

## 5. Files touched

**Modify:**
- `tools/data_tools.py` — C1–C6 (`_coerce_pair`, `_apply_filter`, `_FILTER_OPS`, `_resolve_real_column`/`_normalize`, `_date_key`), plus `filter_op` docstrings (C2).
- `tools/data_viz_tools.py` — D1 (table validation), D2 (docstring).
- `skills/workflow/data_query.md` — C2 (`contains` mention), D3 (un-suppress table path).
- `skills/workflow/data_viz.md` — D2 (table example).
- `.claude/CLAUDE.md` — C6 date-list upkeep + correction.

**Create / extend tests:**
- `tests/test_tools/test_data_tools.py` (or a focused new file) — C1–C5 filter tests + C7 interaction.
- the existing `test_summarize_trend_handles_extended_date_formats` — C6 date cases.
- `tests/test_tools/test_data_tools_function_tool.py` or viz test — D1.
- a server/chart test — D4.

**No changes:** `../CaseReviewChat` (frontend verified complete), `redacting_tool.py` (no auto-distiller table), data profiles, `firewall_client.py`/`safechain_client.py`.

---

## 6. Risks & sequencing

- **Behavior change to `eq` (C1)** is the highest-impact change — it makes string equality forgiving everywhere. Mitigation: scope strictly to the string branch + eq/ne only; numeric/date unaffected; cover with tests asserting numeric/date `eq` unchanged. This matches the user's explicit "be cautious about mismatch / forgiving" intent.
- **Numeric gating (C3)** could, if the regex is wrong, regress legitimate numeric matches. Mitigation: explicit allow/reject test table.
- **Order:** land C first (independent, test-driven), then D (small). C and D do not interact.
- The full test suite currently has **7 pre-existing failures** unrelated to this work (`server.py` prune/warmth helpers; generator `txn_monthly` concept) — do not attribute them to these changes; baseline before starting.
