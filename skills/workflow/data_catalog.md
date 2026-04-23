---
name: Data Catalog
description: How to interpret the case-data catalog and reason about which tables / columns answer a given question — shared by Orchestrator (for team_construction + synthesis) and Data Manager (for governed query access)
type: workflow
owner: [orchestrator, data_manager]
mode: inline
tools: [list_available_tables, get_table_schema, query_table]
---

# Purpose

You are reasoning about structured case data. The case-data catalog describes what tables exist, which columns each has, and what each column means. Use the catalog to decide:

- **Which specialists** should answer a question (Orchestrator team_construction).
- **Which columns** to cite when grounding a synthesized answer (Orchestrator synthesis).
- **Which table + columns** to pull when serving a data request (Data Manager query).

# Catalog surface

Three tools read the catalog at runtime:

- `list_available_tables()` → comma-separated list of table names scoped to the current case.
- `get_table_schema(table_name)` → JSON blob of `{column: {type, description}}` for the named table.
- `query_table(table_name, filter_column, filter_value, filter_op, columns)` → rows matching the filter. See that tool's own docstring for the full operator list (eq / ne / gt / gte / lt / lte / between).

The Orchestrator sees the full catalog context at team-construction time; individual Specialists see per-table schemas on demand.

# How to reason about table choice

1. **Read the question's topic words.** E.g., "bureau score" → a bureau table; "DTI" → income/affordability; "cross-product exposure" → cross-BU.
2. **Scan the roster** (the list of available specialists with their `data_hints`). A specialist's `data_hints` names the tables it owns.
3. **Match topic → table → specialist.** Pick the minimum set of specialists whose `data_hints` collectively cover the question.
4. **For synthesis,** cite data by its actual table+column path (e.g., `bureau.fico_score = 620`), never by vague domain-shorthand.

# Rules

- Do not invent columns that aren't in the catalog. If a question needs a column that's not listed, say so and mark it as a data gap.
- Date columns vary in format across tables (YYYY-MM-DD, YYYY-MM, MonthName'YYYY). Check the schema before writing a filter_value.
- For the wide `model_scores` table (~265 cols), ALWAYS pass `columns=...` to fetch only the subset you need.
- Do not expose raw account numbers, card numbers, or other 6+-digit identifiers in the answer. The Data Manager already applies a redact layer on the returned rows; downstream agents should not undo that masking.
