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
- [`.claude/memory/feedback_commit_only_when_asked.md`](memory/feedback_commit_only_when_asked.md) — auto-commit OK after a feature passes tests (stage specific files, focused message); NEVER auto-push — pushes always wait for the user's explicit order. Report SHA + "not pushed" after committing.
- [`.claude/memory/feedback_alternate_paths_must_replay_full_sse.md`](memory/feedback_alternate_paths_must_replay_full_sse.md) — every branch in `server.py` that emits `final` (cache hit, fallback, retry, error short-circuit) MUST also emit `team_plan` + `agent_started` + `agent_completed` + `chart`; otherwise UI panels stay empty and the user reads it as a silent failure. Recurring class of bug.
- [`.claude/memory/feedback_performance_targets.md`](memory/feedback_performance_targets.md) — user-named wall-clock budgets: screen-rejection < 5s, single-specialist answer ≤ 20s. Treat as failing thresholds, not aspirations; root-cause when exceeded.

When adding a new entry: create the file in `.claude/memory/`, then add a one-line index entry above. Keep file bodies focused (one concern per file).

## Date / time format handling is LOAD-BEARING

Treat date-column parsing as a recurring high-priority concern, not a one-off bug.

- The dev environment ships canonical date formats per data profile (e.g. `model_scores.trans_month` = `YYYY-MM-DD`); the **private/prod environment ships the SAME columns in different formats** (`Jul-25`, `MM/DD/YYYY`, ISO datetimes with time component, etc.).
- When `tools/data_tools.py:_date_key` fails to parse, the downstream tools (`summarize_trend`, date-aware `aggregate_column` op=`min`/`max`) return "no parseable values" → the specialist surfaces that text in `findings` and the analysis is defeated. **This is a user-visible failure mode**, not a silent one.
- When debugging "specialist cannot answer trend / DPD / trajectory" symptoms, **first check the JSONL log for `summarize_trend` returns mentioning "no parseable values"** — sample failing values are logged in the `unparseable_samples` extra. That tells you which format to add to `_date_key`.
- When extending `_date_key`, add a new regex branch + a parametrized test case in `test_summarize_trend_handles_extended_date_formats`. Already covered (do not regress): ISO date / ISO datetime / ISO with slashes / `MM/DD/YYYY` / `DD-MMM-YYYY` / `MonthName-YYYY` / 2-digit year US-slash / compact ISO `YYYYMMDD` / year-only.
- Common gaps to be ready for: `MMM-YY` (`Jul-25`), `MMM-YYYY` (`Jul-2025`), `YYYY-MMM` (`2025-Jul`), pandas timestamps with timezone, Excel serial numbers.
- The skill body (`skills/workflow/data_query.md`) instructs specialists to "match the column's own format; check via `get_table_schema` before passing a filter_value" — keep that wording; never assume a single format.