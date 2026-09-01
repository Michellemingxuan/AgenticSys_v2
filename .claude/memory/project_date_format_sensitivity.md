---
name: project-date-format-sensitivity
description: Date columns across the system come in many formats (private vs dev env, table by table); _date_key in tools/data_tools.py must keep parsing them all, and parsing failures are USER-VISIBLE (the specialist tool returns "no parseable values" which the LLM surfaces in the answer).
type: project
originSessionId: 04fae2a5-b572-455d-b934-90560dd718e8
---
**Date/time format handling is a recurring failure mode and must be treated as load-bearing.**

**Why:** The dev environment ships canonical formats per data profile (e.g. `model_scores.trans_month` = `YYYY-MM-DD`), but the private/prod environment ships the SAME column in different formats (`Jul-25`, `MM/DD/YYYY`, ISO datetimes with time component, etc.). When `_date_key` fails to parse, the affected tools (`summarize_trend`, `aggregate_column` with op=`min`/`max`) return "no parseable values" — and the specialist surfaces that in `findings`, defeating the analysis.

This has been fixed multiple times. Each time a new private-env format appears, the parser needs another branch. The 2026-05-13 occurrence: `model_scores.trans_month` returned 18 records with unrecognized formats; specialist could not parse TSR score trajectory. The 2026-06-10 occurrence: prod `payments.payment_date` ships `D-MMM-YY` (2-digit year, e.g. `7-Jul-24`, `16-Jul-24`) — `_date_key` had `DD-MMM-YYYY` but not the 2-digit-year variant, so `summarize_trend(period='month')` on payments returned an empty series and the spend-vs-payment chart showed ONLY the spend line in prod (worked in dev because dev's payments ship ISO dates). Aggregates on payments still worked (no date bucketing), which is the tell that it was a date-parse failure, not a missing tool call. Same model (gpt-4.1) + same code both envs — purely a data-format difference, not safechain.

**How to apply:**

1. When debugging "specialist cannot answer trend / DPD / trajectory" symptoms, **always check the JSONL log for `viz_render_skipped` / `summarize_trend` returns mentioning "no parseable values"** — that's the smoking gun. Sample values are logged in the `unparseable_samples` extra (added 2026-05-06).

2. When extending `_date_key` in `tools/data_tools.py`, add the new format as another regex + the existing fall-through pattern (cache_key insensitive, normalize string first). Test with `tests/test_tools/test_data_tools.py::test_summarize_trend_handles_extended_date_formats` — parametrize over the new format too.

3. **Surface unparseable samples to the LLM aggressively.** `summarize_trend` already includes sample values in its tool return string; specialists should QUOTE those samples verbatim in `findings`. Don't let the LLM just say "unrecognized format" without naming what it saw — that loses the diagnostic signal.

4. Common formats that have been seen / are likely to appear:
   - Already covered (do not regress): ISO date, ISO datetime, ISO with slashes, MM/DD/YYYY, DD-MMM-YYYY, DD-MMM-YY (2-digit year, e.g. `7-Jul-24`), MonthName-YYYY (4-digit yr), 2-digit-year US slash, compact ISO YYYYMMDD, year-only.
   - Common gaps to add when seen: MMM-YY (`Jul-25`), MMM-YYYY (`Jul-2025`), YYYY-MMM (`2025-Jul`), pandas-style timestamps with timezone, Excel serial date numbers.

5. The skill's `interpretation_guide` should NEVER assume a single date format — always say "match the column's own format; check via `get_table_schema` before passing a filter_value." This rule is in `skills/workflow/data_query.md`.
