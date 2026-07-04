# redacting_tool.py Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the distillation, auto-chart, series-parsing, and KB-digest code out of the 1560-line `agent_factories/redacting_tool.py` into focused `tools/` modules, leaving that file as the redaction + specialist-run wrapper — with zero behavior change.

**Architecture:** Pure move refactor. Functions are transplanted verbatim into new modules; `redacting_tool.py` imports them back so its internal callers (`_runner`, `_distill_and_persist`, `_auto_chart_from_tool_outputs`) keep working. Because the names are re-imported into `redacting_tool`'s namespace, `redacting_tool.<name>` stays a valid attribute throughout — so the suite stays green at every step, even before tests are repointed. Dependency flow is strictly one-way: `redacting_tool → {distiller_pass, auto_chart, kb_tools} → series_extract`.

**Tech Stack:** Python 3.11, pytest, the `autoAI` pyenv virtualenv.

## Global Constraints

- **Behavior-preserving.** No logic, signature, or name changes — functions move verbatim; only their imports get re-homed. The one cleanup: dedupe `_active_kps`.
- **Green-suite gate.** `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q` is **461 passed** today and MUST stay **461 passed** after every task (a pure move changes nothing observable). Same count — not "≥".
- **Interpreter:** always `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python` (never bare `python` — base 3.11.13 lacks matplotlib; see `.claude/memory/dev_env_autoai_interpreter.md`). Never `pip install` / change deps.
- **One-way deps, no cycles.** `series_extract` imports nothing from the other new modules.
- **Names keep their `_` prefix**, imported explicitly across modules.

---

## File Structure

- `tools/series_extract.py` (Create) — shared series plumbing. Depends on: stdlib only.
- `tools/distiller_pass.py` (Create) — the distiller second-pass. Depends on: series_extract, models, skills.loader, tools.node_trace, logger.process_timer, llm.firewall_stack, agents.Runner.
- `tools/auto_chart.py` (Create) — no-LLM chart rendering from series. Depends on: series_extract, tools.viz_renderer.
- `tools/kb_tools.py` (Modify) — gains `_format_kb_digest`; keeps its existing `_active_kps` as the single source.
- `agent_factories/redacting_tool.py` (Modify) — loses ~700 lines; gains four import lines.
- New test files under `tests/test_tools/`.

---

## Task 1: Extract `tools/series_extract.py`

**Files:**
- Create: `tools/series_extract.py`
- Modify: `agent_factories/redacting_tool.py`
- Create: `tests/test_tools/test_series_extract.py`
- Modify: `tests/test_agent_factories/test_redacting_tool.py`

**Interfaces:**
- Produces (module `tools.series_extract`): `_ParsedSeries` (dataclass: `lookup: dict`, `column_name: str`, `key_field: str`, `table_name: str = ""`), `_parse_series_from_tool_outputs(tool_outputs_text: str) -> list[_ParsedSeries]`, `_values_match(a, b) -> bool`, `_fill_kp_numbers(kp_dict: dict, parsed_series: list[_ParsedSeries]) -> None`, `_extract_data_tool_outputs(result) -> str`.

- [ ] **Step 1: Create `tools/series_extract.py`** — move these 5 definitions VERBATIM from `agent_factories/redacting_tool.py`: `_ParsedSeries` (lines 203–209), `_parse_series_from_tool_outputs` (211–311), `_values_match` (314–323), `_fill_kp_numbers` (326–487), `_extract_data_tool_outputs` (856–875). Put this header above them:

```python
"""Series extraction + KP-number filling shared by the distiller pass and the
auto-chart renderer. Parses summarize_trend / batch_summarize_trend /
summarize_by_group tool outputs into `_ParsedSeries`, and fills/constructs a
KnowledgePoint `numbers` array from them. Pure data plumbing — no LLM, no I/O.
Extracted from agent_factories/redacting_tool.py (see
docs/superpowers/specs/2026-07-05-redacting-tool-decomposition-design.md)."""
from __future__ import annotations

import dataclasses
import json
```

- [ ] **Step 2: Rewire `redacting_tool.py`** — DELETE those 5 definitions from `agent_factories/redacting_tool.py`, and add this import near the top (after the existing `from tools.viz_renderer import ...` line):

```python
from tools.series_extract import (
    _ParsedSeries,
    _parse_series_from_tool_outputs,
    _values_match,
    _fill_kp_numbers,
    _extract_data_tool_outputs,
)
```

(`_distill_and_persist`, `_auto_chart_from_tool_outputs`, `_render_auto_charts`, and `_runner` — all still in `redacting_tool.py` at this point — now use the imported versions.)

- [ ] **Step 3: Verify the module imports cleanly**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import tools.series_extract, agent_factories.redacting_tool; print('ok')"`
Expected: `ok` (no ImportError/NameError). If a NameError surfaces inside a moved function, add the missing import to `series_extract.py` from redacting_tool's original import block.

- [ ] **Step 4: Move the tests** — in `tests/test_agent_factories/test_redacting_tool.py`, find every test exercising `_parse_series_from_tool_outputs`, `_fill_kp_numbers`, `_values_match`, `_ParsedSeries`, or `_extract_data_tool_outputs` (grep those names). Move them to a new `tests/test_tools/test_series_extract.py`, changing their import from `from agent_factories.redacting_tool import ...` (or `redacting_tool._x`) to `from tools.series_extract import ...`. Add `tests/test_tools/__init__.py` only if absent.

- [ ] **Step 5: Run the full suite — must be unchanged**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: `461 passed` (same count as baseline). If lower, a test was lost in the move; if errors, a moved import is missing.

- [ ] **Step 6: Commit**

```bash
git add tools/series_extract.py agent_factories/redacting_tool.py tests/test_tools/test_series_extract.py tests/test_agent_factories/test_redacting_tool.py
git commit -m "refactor(tools): extract series_extract from redacting_tool"
```

---

## Task 2: Extract `tools/distiller_pass.py`

**Files:**
- Create: `tools/distiller_pass.py`
- Modify: `agent_factories/redacting_tool.py`
- Create: `tests/test_tools/test_distiller_pass.py`
- Modify: `tests/test_agent_factories/test_redacting_tool.py`

**Interfaces:**
- Consumes: `tools.series_extract._parse_series_from_tool_outputs`, `._fill_kp_numbers` (Task 1).
- Produces (module `tools.distiller_pass`): `_DISTILLER_TIMEOUT_S: float`, `_SERIES_KEYWORDS: frozenset`, `_is_narrow_output(specialist_output, sub_question="") -> bool`, `async _distill_and_persist(app_ctx, name, sub_question, specialist_output, tool_outputs="") -> int`.

- [ ] **Step 1: Create `tools/distiller_pass.py`** — move VERBATIM from `redacting_tool.py`: `_DISTILLER_TIMEOUT_S` (line 52), `_SERIES_KEYWORDS` (127–131), `_is_narrow_output` (134–155), `_distill_and_persist` (878–1101). Header:

```python
"""Distiller second-pass: extract reusable KnowledgePoints from a specialist's
output and persist them to the session KB, filling `numbers` from parsed tool
outputs. Fire-and-forget; scheduled by redacting_tool._runner. Extracted from
agent_factories/redacting_tool.py (see the decomposition design spec)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

from agents import Runner

from llm.firewall_stack import LLM_CALL_KIND
from logger.process_timer import ProcessTimer
from tools.node_trace import _open_node, attach_extra, attach_tag
from tools.series_extract import _parse_series_from_tool_outputs, _fill_kp_numbers
```

(`_DISTILLER_TIMEOUT_S` uses `os.environ`, hence `import os`. If a moved line references a symbol not imported here, add it from redacting_tool's original imports — Step 3 catches it.)

- [ ] **Step 2: Rewire `redacting_tool.py`** — DELETE those 4 definitions; add:

```python
from tools.distiller_pass import _distill_and_persist
```

(Only `_distill_and_persist` is referenced by `redacting_tool._runner` — it's scheduled as `asyncio.create_task(_distill_and_persist(...), name=f"distill-{name}")`. `_is_narrow_output`/`_SERIES_KEYWORDS`/`_DISTILLER_TIMEOUT_S` are used only *inside* `_distill_and_persist`, so they travel with it and are not imported back.) Remove the now-unused `_DISTILLER_TIMEOUT_S` line from redacting_tool if it remains.

- [ ] **Step 3: Verify imports**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import tools.distiller_pass, agent_factories.redacting_tool; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Move the tests** — move every test in `test_redacting_tool.py` exercising `_distill_and_persist`, `_is_narrow_output`, or `_SERIES_KEYWORDS` into `tests/test_tools/test_distiller_pass.py`, repointing imports to `from tools.distiller_pass import ...`. (These are the same tests fixed for `asyncio.run` earlier — keep that pattern.)

- [ ] **Step 5: Run the full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: `461 passed`.

- [ ] **Step 6: Commit**

```bash
git add tools/distiller_pass.py agent_factories/redacting_tool.py tests/test_tools/test_distiller_pass.py tests/test_agent_factories/test_redacting_tool.py
git commit -m "refactor(tools): extract distiller_pass from redacting_tool"
```

---

## Task 3: Extract `tools/auto_chart.py`

**Files:**
- Create: `tools/auto_chart.py`
- Modify: `agent_factories/redacting_tool.py`
- Create: `tests/test_tools/test_auto_chart.py`
- Modify: `tests/test_agent_factories/test_redacting_tool.py`

**Interfaces:**
- Consumes: `tools.series_extract._ParsedSeries`, `._parse_series_from_tool_outputs` (Task 1); `tools.viz_renderer` (`kp_to_vega_spec`, `render_chart`, `_infer_unit`).
- Produces (module `tools.auto_chart`): `async _auto_chart_from_tool_outputs(app_ctx, name, tool_outputs) -> int`, `_render_auto_charts(trend_series, group_series, name, charts_dir, kb, turn_id, catalog, logger, emit_event=None) -> int`.

- [ ] **Step 1: Create `tools/auto_chart.py`** — move VERBATIM: `_auto_chart_from_tool_outputs` (489–564) and `_render_auto_charts` (567–853). Header:

```python
"""Auto-chart renderer: build charts from a specialist's parsed tool-output
series with NO LLM. Scheduled fire-and-forget by redacting_tool._runner in
parallel with the distiller. Extracted from agent_factories/redacting_tool.py
(see the decomposition design spec)."""
from __future__ import annotations

from pathlib import Path

from tools.series_extract import _ParsedSeries, _parse_series_from_tool_outputs
from tools.viz_renderer import kp_to_vega_spec, render_chart, _infer_unit
```

(The originals import `kp_to_vega_spec, render_chart` inside the function bodies and `_infer_unit` mid-function — consolidate to the module top as shown. If a moved line references another symbol, add its import; Step 3 catches misses.)

- [ ] **Step 2: Rewire `redacting_tool.py`** — DELETE those 2 definitions; add:

```python
from tools.auto_chart import _auto_chart_from_tool_outputs
```

(`_runner` schedules `_auto_chart_from_tool_outputs` as `asyncio.create_task(..., name=f"autochart-{name}")`; `_render_auto_charts` is called only by it.)

- [ ] **Step 3: Verify imports**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import tools.auto_chart, agent_factories.redacting_tool; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Move the tests** — move tests exercising `_auto_chart_from_tool_outputs` / `_render_auto_charts` from `test_redacting_tool.py` to `tests/test_tools/test_auto_chart.py`, repointing imports to `from tools.auto_chart import ...`.

- [ ] **Step 5: Run the full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: `461 passed`.

- [ ] **Step 6: Commit**

```bash
git add tools/auto_chart.py agent_factories/redacting_tool.py tests/test_tools/test_auto_chart.py tests/test_agent_factories/test_redacting_tool.py
git commit -m "refactor(tools): extract auto_chart from redacting_tool"
```

---

## Task 4: Move KB digest into `tools/kb_tools.py` + dedupe `_active_kps`

**Files:**
- Modify: `tools/kb_tools.py`
- Modify: `agent_factories/redacting_tool.py`
- Modify: `tests/test_agent_factories/test_redacting_tool.py` and/or `tests/test_tools/test_kb_tools.py`

**Interfaces:**
- Consumes: `tools.kb_tools._active_kps` (already exists there).
- Produces (module `tools.kb_tools`): `_format_kb_digest(kps, full_kb=None, self_name=None) -> str` (moved), and `_active_kps` remains the single canonical definition.

- [ ] **Step 1: Confirm the `_active_kps` bodies are identical** — before deleting redacting_tool's copy, verify it matches kb_tools':

Run: `diff <(sed -n '61,74p' agent_factories/redacting_tool.py) <(sed -n '21,28p' tools/kb_tools.py)`
Expected: only whitespace/line-number differences (same logic). If the bodies differ materially, STOP and flag — dedupe assumption is wrong.

- [ ] **Step 2: Add `_format_kb_digest` to `tools/kb_tools.py`** — move it VERBATIM from `redacting_tool.py` (76–117). It calls `_active_kps` — which already lives in `kb_tools.py`, so no extra import.

- [ ] **Step 3: Rewire `redacting_tool.py`** — DELETE `_format_kb_digest` (76–117) AND `_active_kps` (61–74); add:

```python
from tools.kb_tools import _active_kps, _format_kb_digest
```

(`_runner` uses both `_format_kb_digest` and `_active_kps`.)

- [ ] **Step 4: Verify imports**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import tools.kb_tools, agent_factories.redacting_tool; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Move any `_format_kb_digest` tests** — if `test_redacting_tool.py` has tests for `_format_kb_digest`, move them to `tests/test_tools/test_kb_tools.py`, repointing to `from tools.kb_tools import _format_kb_digest`. (`_active_kps` tests, if any, likely already live with kb_tools.)

- [ ] **Step 6: Run the full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: `461 passed`.

- [ ] **Step 7: Commit**

```bash
git add tools/kb_tools.py agent_factories/redacting_tool.py tests/test_agent_factories/test_redacting_tool.py tests/test_tools/test_kb_tools.py
git commit -m "refactor(tools): move _format_kb_digest to kb_tools; dedupe _active_kps"
```

---

## Task 5: Final verification

**Files:** none (verification only; fold into Task 4's commit if clean on first run).

- [ ] **Step 1: Confirm nothing moved remains in `redacting_tool.py`**

Run: `grep -nE "def _(distill_and_persist|parse_series_from_tool_outputs|fill_kp_numbers|values_match|auto_chart_from_tool_outputs|render_auto_charts|is_narrow_output|extract_data_tool_outputs|format_kb_digest|active_kps)\b|class _ParsedSeries" agent_factories/redacting_tool.py`
Expected: no matches.

- [ ] **Step 2: Confirm redacting_tool shrank and still holds its own concerns**

Run: `grep -nE "^(async )?def |^class " agent_factories/redacting_tool.py`
Expected: only `redacting_tool`, `_runner` (nested), `_record_failure`, `_normalize_subq`, `_compact_specialist_history`. Line count well under ~900.

- [ ] **Step 3: No import cycles + full suite one more time**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: `461 passed`. Also: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -c "import tools.series_extract, tools.distiller_pass, tools.auto_chart, tools.kb_tools, agent_factories.redacting_tool; print('no cycles')"` → `no cycles`.

- [ ] **Step 4: Commit (if any verification edits were needed)**

```bash
git add -A && git commit -m "refactor: finalize redacting_tool decomposition (verification)"
```

---

## Self-Review

**Spec coverage:**
- §3 module layout → Tasks 1–4 create the four modules.
- §4 function inventory → each row maps to a task step (series→T1, distiller→T2, auto_chart→T3, kb→T4, stays→untouched).
- §5 consumer wiring → the import-back lines in T1–T4 Step 2.
- §6 `_active_kps` dedupe → T4 Steps 1 + 3.
- §7 tests → the "Move the tests" step in each task.
- §8 verification → Task 5 + the 461 gate in every task.

**Placeholder scan:** No "TBD"/"handle edge cases". For a verbatim move, the instruction is exact source line ranges + destination + the precise import block to add + the import-back line — that IS the complete content (reproducing 700 lines of unchanged bodies in the plan would be noise). The "add any missing import" fallback is bounded by the import-verify step that immediately follows each move.

**Type consistency:** Module/function names are consistent across tasks — `tools.series_extract` (`_parse_series_from_tool_outputs`, `_fill_kp_numbers`, `_ParsedSeries`, `_values_match`, `_extract_data_tool_outputs`), `tools.distiller_pass` (`_distill_and_persist`, `_is_narrow_output`, `_SERIES_KEYWORDS`, `_DISTILLER_TIMEOUT_S`), `tools.auto_chart` (`_auto_chart_from_tool_outputs`, `_render_auto_charts`), `tools.kb_tools` (`_format_kb_digest`, `_active_kps`) — matching the spec's §4 inventory and §5 wiring exactly.
