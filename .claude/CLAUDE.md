- The agents generally have the permissions for bash and can execute "cd" directly.
- When establishing the framwork from scratch, there operations also can be executed directly.
- Only operations that modify files (e.g., `edit`, `write`, `create`) require prior confirmation. 
- Actions such as reading, viewing, searching, or running read-only commands should be executed directly without asking.

## Project memory (`.claude/memory/`)

Project-scoped, version-controlled memory store. Travels with this repo. Consult the relevant entry below by `Read`-ing the file the first time its topic comes up in a conversation.

- [`.claude/memory/safechain_dual_environment.md`](memory/safechain_dual_environment.md) — private/prod uses safechain (no native tool-calls, single combined message); dev uses OpenAI API + openai-agents SDK directly. v2's SDK-based wiring is dev-only without an adapter.
- [`.claude/memory/data_agent_future_vision.md`](memory/data_agent_future_vision.md) — planned: central-DB big-data queries replace the in-memory simulated gateway; current `SimulatedDataGateway` is a stand-in.
- [`.claude/memory/project_date_format_sensitivity.md`](memory/project_date_format_sensitivity.md) — `_date_key` parser failures cascade into specialist "no parseable values" findings; recurring across private/dev format drift. Extend per format, quote samples in findings. (Full content also embedded in this CLAUDE.md below — see "Date / time format handling is LOAD-BEARING.")
- [`.claude/memory/workflow_preferences.md`](memory/workflow_preferences.md) — prefer direct Edit/Bash execution over Task-tool subagent dispatches; subagents trigger permission prompts that create friction.
- [`.claude/memory/feedback_commit_only_when_asked.md`](memory/feedback_commit_only_when_asked.md) — NEVER auto-commit or auto-push. Wait for the user to say "commit" / "push" / similar in the CURRENT turn. Even after a feature passes tests, end the turn with an offer ("Want me to commit?"), not a commit. (The brief mid-day relaxation today was walked back.)
- [`.claude/memory/feedback_alternate_paths_must_replay_full_sse.md`](memory/feedback_alternate_paths_must_replay_full_sse.md) — every branch in `server.py` that emits `final` (cache hit, fallback, retry, error short-circuit) MUST also emit `team_plan` + `agent_started` + `agent_completed` + `chart`; otherwise UI panels stay empty and the user reads it as a silent failure. Recurring class of bug.
- [`.claude/memory/feedback_performance_targets.md`](memory/feedback_performance_targets.md) — user-named wall-clock budgets: screen-rejection < 5s, single-specialist answer ≤ 20s. Treat as failing thresholds, not aspirations; root-cause when exceeded.
- [`.claude/memory/feedback_plots_preference.md`](memory/feedback_plots_preference.md) — horizontal `share` bars for category breakdowns (readable labels); sort by rank/temporal; uniform time intervals for unwindowed questions.
- [`.claude/memory/feedback_vegalite_text_labels.md`](memory/feedback_vegalite_text_labels.md) — Vega-Lite positioned text labels must use inline `data.values` + field encoding; `stroke`/`paintOrder`/`fontWeight:600` all fail.
- [`.claude/memory/feedback_openai_safechain_parity.md`](memory/feedback_openai_safechain_parity.md) — any change to `firewall_client.py` (OpenAI path) must be mirrored in `safechain_client.py` (SafeChain/prod path). They are parallel implementations.
- [`.claude/memory/project_bureau_credit_scores.md`](memory/project_bureau_credit_scores.md) — FICO (v7/v8, unsecured-focused) is the primary consumer credit score; LN (LexisNexis) is supplementary. FSS/CSS/Paydex/LN-Business are commercial-entity scores. For small-biz owners, consumer + business scores are treated together.
- [`.claude/memory/transaction_vs_monthly_tables.md`](memory/transaction_vs_monthly_tables.md) — transaction-level tables (model_scores_transaction / score_drivers_transaction) answer per-txn & approve-deny questions; monthly tables answer trends; filter day-grain `trans_dt` by default, `txn_date_time` only for within-day precision.
- [`.claude/memory/safechain_async_and_thread_occupation.md`](memory/safechain_async_and_thread_occupation.md) — "stuck at team construction" / "input not captured" are caused by hung sync `chain.invoke()` in worker threads that can't be released; fix is async-native `await llm.ainvoke()` + per-call `asyncio.wait_for` (SafeChain model is real-async via `_agenerate`/`_astream`). Don't naively restore f15d6b9's reverted thread pool.

When adding a new entry: create the file in `.claude/memory/`, then add a one-line index entry above. Keep file bodies focused (one concern per file).

## Date / time format handling is LOAD-BEARING

Treat date-column parsing as a recurring high-priority concern, not a one-off bug.

- The dev environment ships canonical date formats per data profile (e.g. `model_scores.trans_month` = `YYYY-MM-DD`); the **private/prod environment ships the SAME columns in different formats** (`Jul-25`, `MM/DD/YYYY`, ISO datetimes with time component, etc.).
- When `tools/data_tools.py:_date_key` fails to parse, the downstream tools (`summarize_trend`, date-aware `aggregate_column` op=`min`/`max`) return "no parseable values" → the specialist surfaces that text in `findings` and the analysis is defeated. **This is a user-visible failure mode**, not a silent one.
- When debugging "specialist cannot answer trend / DPD / trajectory" symptoms, **first check the JSONL log for `summarize_trend` returns mentioning "no parseable values"** — sample failing values are logged in the `unparseable_samples` extra. That tells you which format to add to `_date_key`.
- When extending `_date_key`, add a new regex branch + a parametrized test case in `test_summarize_trend_handles_extended_date_formats`. Already covered (do not regress): ISO date / ISO datetime / ISO with slashes / `MM/DD/YYYY` / `DD-MMM-YYYY` / `MonthName-YYYY` / 2-digit year US-slash / compact ISO `YYYYMMDD` / year-only / `MMM-YY` (`Jul-25`) / `MMM-YYYY` (`Jul-2025`) / `YYYY-MMM` (`2025-Jul`) / pandas timestamps with timezone / Excel serial numbers (5-digit integer in 20000–60000).
- Note: `_date_key` collapses datetimes to day-grain, so a `2024-01-01` filter DOES match a `2024-01-01 15:25:20` cell.
- Common gaps to be ready for: `YYYY/MM/DD`, epoch milliseconds, locale-specific month abbreviations beyond English.
- The skill body (`skills/workflow/data_query.md`) instructs specialists to "match the column's own format; check via `get_table_schema` before passing a filter_value" — keep that wording; never assume a single format.