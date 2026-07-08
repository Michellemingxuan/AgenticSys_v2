# `tools/agent_tools/` Package — Design Spec

Date: 2026-07-08
Status: Draft (for review)
Owner: tools / agent_factories
Prior art: `docs/superpowers/specs/2026-07-05-redacting-tool-decomposition-design.md` (Phase 1)

## 1. Problem

`tools/redacting_tool.py` (603 lines) is named for redaction, but redaction is
~2 lines of it (a `sanitize_message` on input, a `redact_payload` on output —
both calls into `llm/firewall_stack`). Its real job is **"wrap an inner Agent as
a tool: build its input, run it, process its output."** The misleading name is
compounded by the fact that the module and its post-run machinery
(`distiller_pass`, `auto_chart`, `series_extract`, extracted in Phase 1) live
loose in `tools/` next to genuinely agent-facing function tools (`data_tools`,
`kb_tools`, `fs_tools`, `data_viz_tools`), with no signal about which is which.

This is Phase 2 of the decomposition: the "deeper reframe / file rename (Option
B)" that the 2026-07-05 spec explicitly **deferred** ("No function renames… no
file rename. Deferred.").

## 2. Goals / Non-goals

**Goals**
- Rename the wrapper to reflect its job: `redacting_tool.py` → `agent_tool.py`,
  `redacting_tool()` → `agent_tool()`. Redaction becomes a documented property,
  not the name.
- Introduce a `tools/agent_tools/` package holding the **agent-construction /
  running machinery** — the wrapper plus the fire-and-forget modules the wrapper
  schedules — so it is visibly separated from agent-facing tools.
- Extract the cohesive Layer-5 input builders into
  `tools/agent_tools/specialist_input_tool.py`.
- Keep dependency flow strictly one-way (acyclic); each unit independently
  testable.

**Non-goals (explicitly out of scope)**
- **No behavior changes, no signature changes.** Function bodies move verbatim.
- **No decomposition of the `_runner` closure** (its internal structure is
  unchanged). Only module boundaries and names change.
- **No change to agent-facing tools** (`data_tools`, `kb_tools`, `fs_tools`,
  `data_viz_tools`, `viz_renderer`, `episodic`) beyond import-path updates where
  they consume moved code (none do — they are leaves).

## 3. Taxonomy — the rule this refactor encodes

Two categories, distinguished by **who invokes the code**:

- **Agent-facing tools** — invoked by an agent via its skills / tool-calling
  (`@function_tool`). These stay **directly under `tools/`**.
- **Agent-construction / running machinery** — used by the *system* to build,
  wrap, and run agents; never invoked by an agent. These move into
  **`tools/agent_tools/`**.

The split is clean because "make chart" and "handle memory" each exist in both
forms: the agent-facing version stays; the machinery version moves.

| Capability | Agent-facing (stays in `tools/`) | Machinery (moves to `agent_tools/`) |
|---|---|---|
| charting | `data_viz_tools.make_chart` | `auto_chart` (fire-and-forget, no LLM) |
| memory | `kb_tools.kb_lookup` / `kb_list_topics` | `distiller_pass` (fire-and-forget distill) |
| — | `data_tools` (query), `fs_tools` (read reports) | `agent_tool` (wrap agent as tool), `specialist_input_tool` (build wrapped-agent input) |

`kb_tools.py` staying in `tools/` is the rule working correctly, not an
exception: it is `@function_tool` code the specialists call, and it is also
imported by `agent_factories/specialist_agent.py` (an external consumer of the
cluster).

## 4. Target module layout

```
tools/agent_tools/                     ← new package
    __init__.py                        re-exports: `from .agent_tool import agent_tool`
    agent_tool.py                      (was tools/redacting_tool.py)
        agent_tool()                   (was redacting_tool())
        _runner                        the closure — unchanged
        _normalize_subq                stays inline (closure-bound)
        _record_failure                stays inline (closure-bound)
        specialist constants           _SPECIALIST_MAX_TURNS, _SPECIALIST_TIMEOUT_S, etc.
    specialist_input_tool.py           (new)
        _compose_specialist_input
        _render_directed_variables
        _compact_specialist_history
        _SPECIALIST_HISTORY_KEEP_RECENT_USER_MESSAGES
        _ELIDED_SPECIALIST_TOOL_OUTPUT
    distiller_pass.py                  (moved from tools/)
    auto_chart.py                      (moved from tools/)
    series_extract.py                  (moved from tools/)

tools/                                 ← agent-facing tools + shared leaves (unchanged except moves out)
    data_tools.py  data_viz_tools.py  fs_tools.py  kb_tools.py
    viz_renderer.py  episodic.py  acropedia.py  timing_report.py  node_trace/
```

**Dependency flow (strictly one-way, no cycles):**
```
tools/agent_tools/agent_tool
   → tools/agent_tools/{specialist_input_tool, distiller_pass, auto_chart}
   → tools/agent_tools/series_extract
   → tools/{kb_tools, viz_renderer, episodic}      (leaves in tools/)
```
`agent_tools/*` may import from `tools/*`; `tools/*` never imports from
`agent_tools/*`. `agent_factories/orchestrator_agent.py` imports from
`tools.agent_tools`.

## 5. File / symbol inventory (source → destination)

| Symbol / file | Source | Destination |
|---|---|---|
| `redacting_tool()` → `agent_tool()`, `_runner`, `_normalize_subq`, `_record_failure`, specialist constants | `tools/redacting_tool.py` | `tools/agent_tools/agent_tool.py` |
| `_compose_specialist_input` | `tools/redacting_tool.py:47` | `tools/agent_tools/specialist_input_tool.py` |
| `_render_directed_variables` | `tools/redacting_tool.py:59` | `specialist_input_tool.py` |
| `_compact_specialist_history` (+ its 2 module constants) | `tools/redacting_tool.py:70,40,41` | `specialist_input_tool.py` |
| `distiller_pass.py` (whole file) | `tools/distiller_pass.py` | `tools/agent_tools/distiller_pass.py` |
| `auto_chart.py` (whole file) | `tools/auto_chart.py` | `tools/agent_tools/auto_chart.py` |
| `series_extract.py` (whole file) | `tools/series_extract.py` | `tools/agent_tools/series_extract.py` |

Imports use the codebase's existing **absolute** style
(`from tools.agent_tools.series_extract import …`), for consistency and
grep-ability, matching how the Phase-1 modules already reference each other.

## 6. Reference updates

**Production (the only non-test/non-comment call site):**
- `agent_factories/orchestrator_agent.py`
  - `from tools.redacting_tool import redacting_tool` → `from tools.agent_tools import agent_tool`
  - two calls `redacting_tool(...)` → `agent_tool(...)` (lines 160, 165)

**Intra-cluster imports** (in the moved modules): repoint
`tools.series_extract` → `tools.agent_tools.series_extract`, and `agent_tool`'s
imports of `tools.distiller_pass` / `tools.auto_chart` →
`tools.agent_tools.*`. Its imports of `tools.kb_tools`, `tools.viz_renderer`,
`tools.episodic` are unchanged.

**Tests** (repoint imports + patch targets; move files to a mirrored
`tests/test_tools/test_agent_tools/` package):
- `test_redacting_tool.py` → `test_agent_tools/test_agent_tool.py`;
  `from tools.redacting_tool import redacting_tool` → `agent_tool`; every
  `patch("tools.redacting_tool.Runner.run")` /
  `patch("tools.redacting_tool.asyncio.wait_for")` → `tools.agent_tools.agent_tool.*`;
  the `_compact_specialist_history` import → `tools.agent_tools.specialist_input_tool`.
- `test_distiller_pass.py`, `test_auto_chart.py`, `test_series_extract.py` →
  `test_agent_tools/`; repoint imports to `tools.agent_tools.*`.
- `test_episodic_specialist_slice.py`, `test_redacting_directed_vars.py`:
  repoint `_compose_specialist_input` / `_render_directed_variables` imports to
  `tools.agent_tools.specialist_input_tool`.

**Doc-comments** (~20 mentions of the name `redacting_tool` in `server.py`,
`agent_factories/app_context.py`, `distiller_agent.py`, `orchestrator.py`,
`kb_tools.py`, `viz_renderer.py`, `firewall_stack.py`, `models/types.py`,
`node_trace/hooks.py`, `series_extract.py`, `auto_chart.py`, `distiller_pass.py`,
and `notebooks/_build_test_chat_mode.py`): replace `redacting_tool` →
`agent_tool` in prose. Non-functional, but leaving them perpetuates the exact
misnomer this refactor removes. `brainstorm/` artifacts are left as historical
record.

## 7. Tests / verification (behavior preservation is the whole point)

Run with the **autoAI** interpreter
(`~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`); bare
`python` lacks matplotlib and errors at collection (see
`.claude/memory/dev_env_autoai_interpreter.md`).

1. **Baseline capture:** record the pass count on `main`/pre-refactor
   (`pytest tests/ -q`). A pure move must yield the **same** count after.
2. **Post-refactor:** identical pass count; zero collection errors (catches
   broken imports / cycles).
3. **Grep gates:**
   - `grep -rn "tools.redacting_tool\|import redacting_tool" . --include=*.py`
     → no matches outside `brainstorm/`.
   - `grep -rn "def _compose_specialist_input\|def _render_directed_variables\|def _compact_specialist_history" tools/agent_tools/agent_tool.py`
     → no matches (all in `specialist_input_tool.py`).
4. **Standalone import:** each new module imports cleanly under autoAI
   (`python -c "import tools.agent_tools.agent_tool"` etc.).

## 8. Files touched

- **Create:** `tools/agent_tools/__init__.py`,
  `tools/agent_tools/specialist_input_tool.py`,
  `tests/test_tools/test_agent_tools/__init__.py`.
- **Move (git mv):** `tools/redacting_tool.py` → `tools/agent_tools/agent_tool.py`;
  `tools/{distiller_pass,auto_chart,series_extract}.py` → `tools/agent_tools/`;
  the four test files → `tests/test_tools/test_agent_tools/`.
- **Modify:** `agent_factories/orchestrator_agent.py` (import + 2 calls); moved
  modules' intra-cluster imports; moved tests' imports + patch targets;
  `test_episodic_specialist_slice.py` + `test_redacting_directed_vars.py`
  imports; ~20 doc-comments.

## 9. Risks

- **Import wiring / cycles** — mitigated by the strict one-way layout (§4);
  caught by collection-time import errors and the standalone-import gate.
- **Missed patch target in a moved test** — a stale
  `patch("tools.redacting_tool.Runner.run")` silently patches nothing and the
  test's stub never binds; caught by the test behaving differently (assertion
  failure), and by the §7.3 grep for the old path.
- **`git mv` + edit ordering** — move first (preserve history), then edit
  imports, so blame/history survive the rename.
- **Doc-comment churn is broad but non-functional** — do it as a final
  find/replace pass; it cannot affect the test count, so §7.1/§7.2 still gate
  correctness.
