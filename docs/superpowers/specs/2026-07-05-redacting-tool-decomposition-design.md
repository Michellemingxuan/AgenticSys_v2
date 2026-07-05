# redacting_tool.py Decomposition — Design Spec

Date: 2026-07-05
Status: Draft (for review)
Owner: agent_factories / tools

## 1. Problem

`agent_factories/redacting_tool.py` is **1560 lines**. Its name promises "wrap an
agent with PII redaction," but it has accreted four unrelated subsystems:
distillation, auto-charting, series parsing, and KB-digest rendering. That's a
single-responsibility violation — the distiller/chart mass (~700 lines) has
nothing to do with redaction. Extract those subsystems into focused `tools/`
modules so the file matches its purpose.

## 2. Goals / Non-goals

**Goals**
- Pure structural refactor: move the distiller / auto-chart / series-parsing /
  KB-digest code into `tools/`, leaving `redacting_tool.py` as the
  redaction-plus-specialist-run wrapper.
- Clean, one-way (acyclic) module boundaries; each unit independently testable.
- Dedupe `_active_kps` (currently copied in both `redacting_tool.py` and
  `tools/kb_tools.py`) to a single source.

**Non-goals (explicitly out of scope)**
- **No behavior changes, no signature changes.** Function bodies move verbatim.
- **No function renames** — names keep their `_` prefix and are imported
  explicitly across modules (decision (a); lowest churn, revisit later).
- **No deeper reframe** of the specialist-run wrapper / no file rename (the
  earlier "Option B"). Deferred.

## 3. Target module layout

```
tools/series_extract.py     ← shared series plumbing (leaf: stdlib + json only)
    _ParsedSeries
    _parse_series_from_tool_outputs
    _values_match
    _fill_kp_numbers
    _extract_data_tool_outputs

tools/distiller_pass.py      → imports series_extract (+ models, skills.loader,
    _DISTILLER_TIMEOUT_S            tools.node_trace, logger.process_timer,
    _SERIES_KEYWORDS               llm.firewall_stack)
    _is_narrow_output
    _distill_and_persist

tools/auto_chart.py          → imports series_extract + tools.viz_renderer
    _auto_chart_from_tool_outputs
    _render_auto_charts

tools/kb_tools.py (existing) → gains _format_kb_digest; _active_kps becomes the
    _active_kps  (single source)   ONE definition (its existing copy stays,
    _format_kb_digest              redacting_tool's copy is deleted)

agent_factories/redacting_tool.py  → imports the four modules above
    redacting_tool, _runner, _record_failure, _normalize_subq,
    _compact_specialist_history,
    _SPECIALIST_MAX_TURNS, _SPECIALIST_TIMEOUT_S,
    _SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES, _ELIDED_SPECIALIST_TOOL_OUTPUT
```

Dependency flow: `redacting_tool → {distiller_pass, auto_chart, kb_tools} →
series_extract`. Strictly one-way; no cycles (`viz_renderer` is already a leaf,
`kb_tools` imports nothing from the others).

## 4. Function inventory (source → destination)

| Function / const | redacting_tool.py lines | Destination |
|---|---|---|
| `_ParsedSeries` | 203–209 | `tools/series_extract.py` |
| `_parse_series_from_tool_outputs` | 211–311 | series_extract |
| `_values_match` | 314–323 | series_extract |
| `_fill_kp_numbers` | 326–487 | series_extract |
| `_extract_data_tool_outputs` | 856–875 | series_extract |
| `_DISTILLER_TIMEOUT_S` | 52 | `tools/distiller_pass.py` |
| `_SERIES_KEYWORDS` | 127–131 | distiller_pass |
| `_is_narrow_output` | 134–155 | distiller_pass |
| `_distill_and_persist` | 878–1101 | distiller_pass |
| `_auto_chart_from_tool_outputs` | 489–564 | `tools/auto_chart.py` |
| `_render_auto_charts` | 567–853 | auto_chart |
| `_format_kb_digest` | 76–117 | `tools/kb_tools.py` |
| `_active_kps` | 61–74 | **deleted** (dedupe — use kb_tools' copy) |
| `_compact_specialist_history` | 158–199 | **stays** |
| `_record_failure` | 1104–1146 | **stays** |
| `_normalize_subq` | 1148–1154 | **stays** |
| `redacting_tool` / `_runner` | 1157–1560 | **stays** |
| specialist constants | 30, 37, 54, 55 | **stays** |

Notes: `_is_narrow_output` (uses `_SERIES_KEYWORDS`) and `_fill_kp_numbers` /
`_parse_series_from_tool_outputs` are all called *inside* `_distill_and_persist`;
the auto-chart path also uses `_parse_series_from_tool_outputs` — hence
series_extract is the shared module both import. `_distill_and_persist` does
**not** render charts (that moved to auto_chart already), so distiller_pass does
not depend on viz_renderer.

## 5. Consumer wiring (`_runner`)

`_runner` in `redacting_tool.py` currently calls these directly; after the move
it imports them:
- `_extract_data_tool_outputs` ← `tools.series_extract`
- `_distill_and_persist` ← `tools.distiller_pass`
- `_auto_chart_from_tool_outputs` ← `tools.auto_chart`
- `_format_kb_digest`, `_active_kps` ← `tools.kb_tools`

The fire-and-forget scheduling (`asyncio.create_task(..., name="distill-{name}")`
/ `"autochart-{name}"`) stays in `_runner` — only the coroutines it schedules
move. This preserves the server-side re-dispatch hygiene
(`_invalidate_specialist_distillation` keys on those exact task names).

## 6. `_active_kps` dedupe

`_active_kps` is defined identically in `redacting_tool.py:61` and
`tools/kb_tools.py:21`. Keep the `kb_tools.py` copy as the single source; delete
`redacting_tool.py`'s; `_runner` imports it from `kb_tools`. Verify the two
bodies are byte-identical before deleting (they are, per inspection — confirm in
implementation).

## 7. Tests

Move the distiller / series / auto-chart tests out of
`tests/test_agent_factories/test_redacting_tool.py` into:
- `tests/test_tools/test_series_extract.py`
- `tests/test_tools/test_distiller_pass.py`
- `tests/test_tools/test_auto_chart.py`

repointing imports from `redacting_tool` to the new modules. Specialist-wrapper /
redaction tests stay in `test_redacting_tool.py`. Any other test referencing a
moved symbol via `redacting_tool.<name>` (e.g. `_parse_series_from_tool_outputs`,
`_fill_kp_numbers`, `_distill_and_persist`) is repointed to the new module.

## 8. Verification (behavior preservation is the whole point)

- Baseline: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
  is **461 passed** today. After the refactor it must be **461 passed** (same
  count) — a pure move changes nothing observable.
- `grep -nE "def _(distill|parse_series|fill_kp|values_match|auto_chart|render_auto|is_narrow|extract_data)"
  agent_factories/redacting_tool.py` → no matches (all moved).
- No import cycles: importing each new module standalone succeeds under autoAI.
- Run tests with the **autoAI** interpreter (matplotlib present); see
  `.claude/memory/dev_env_autoai_interpreter.md`.

## 9. Files touched

- **Create:** `tools/series_extract.py`, `tools/distiller_pass.py`,
  `tools/auto_chart.py`, `tests/test_tools/test_series_extract.py`,
  `tests/test_tools/test_distiller_pass.py`, `tests/test_tools/test_auto_chart.py`.
- **Modify:** `agent_factories/redacting_tool.py` (delete moved code, add imports),
  `tools/kb_tools.py` (add `_format_kb_digest`), `tests/test_agent_factories/test_redacting_tool.py` (trim moved tests).

## 10. Risks

- **Import wiring / cycles** — mitigated by the strict one-way layout; verified in §8.
- **A moved function's transitive imports** (node_trace, ProcessTimer,
  LLM_CALL_KIND, viz_renderer) must travel to the right module — caught by import
  errors at collection.
- **Missed `redacting_tool.<name>` reference in a test** — caught by the suite;
  §8's grep + the 461-count gate surface any straggler.
