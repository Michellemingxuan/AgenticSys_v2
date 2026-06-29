# Report-Needle `fs_grep` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the report agent a content-search tool (`fs_grep`) plus a ranged `fs_read_file`, so it can locate and slice-read the relevant section of a single long consolidated report file — while keeping the fast Concept → File table routing primary for the canonical multi-file layout.

**Architecture:** `fs_grep(terms)` does a case-insensitive multi-term OR substring search across all case-folder files and returns line-numbered, ranked snippets. `fs_read_file` gains optional `start_line`/`end_line` so the agent reads only the window grep points at. `report_needle.md` gains a path-selection step (multi-file → table; one long file → grep + slice read). The report agent is wired with the new tool.

**Tech Stack:** Python 3.11, `openai-agents` 0.3.3 (`@function_tool`, `RunContextWrapper[AppContext]`), stdlib `re`, pytest + pytest-asyncio.

## Global Constraints

- **No new dependencies.** stdlib `re` only — no fuzzy/embedding libraries.
- **Case-folder confinement.** All file access stays inside `ctx.context.case_folder`; never shell out to the Unix `grep` binary.
- **Strict-schema-safe tool signatures.** Use sentinel defaults (`int = 0`, `str = ""`) like the existing tools — NOT `int | None` / `Optional` — so tools stay valid under the SDK's default strict mode.
- **Numeric parity.** Every file read (grep snippets AND ranged reads) must pass through `_format_long_numerics` so values match what the reviewer ultimately quotes (`$174,897.36`) and so grep line numbers align with `fs_read_file` line numbers.
- **Grep is discovery-only.** It never replaces a read; the agent still reads (a full file or a slice) before drafting `ReportDraft`. The `ReportDraft` output contract is unchanged.
- **Performance budget.** Single-specialist answer ≤ 20s; this design must stay latency-neutral for the multi-file path (no extra round-trip there).
- **Commits.** Do NOT auto-commit or push. Stage changes and pause for the user's explicit go-ahead before each commit (project rule).

---

### Task 1: `fs_grep` content-search tool

**Files:**
- Modify: `tools/fs_tools.py` (add module constants + `fs_grep` after `fs_read_file`, ~line 76)
- Test: `tests/test_tools/test_fs_tools.py`

**Interfaces:**
- Consumes: `_format_long_numerics(text: str) -> str` (existing, `tools/fs_tools.py:27`); `AppContext.case_folder: Path`.
- Produces: `fs_grep` (a `FunctionTool`, name `"fs_grep"`) wrapping `async def fs_grep(ctx: RunContextWrapper[AppContext], terms: list[str]) -> str`. Returns a ranked, line-numbered match report; `"No terms provided."` / `"No matches for: [...]"` / `"No case folder available."` for the empty/miss/no-folder cases. Snippet lines are formatted `  L<n>: <text>` with 1-based line numbers.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools/test_fs_tools.py` (update the import on line 12 to `from tools.fs_tools import fs_grep, fs_list_files, fs_read_file`):

```python
@pytest.mark.asyncio
async def test_fs_grep_or_matching_and_ranking(tmp_path):
    # file_hi matches 3 distinct terms across 2 lines; file_lo matches 1.
    (tmp_path / "file_hi.md").write_text(
        "Recurring spend pattern observed.\n"
        "A high-value transaction of 174897 posted.\n"
    )
    (tmp_path / "file_lo.md").write_text("Only one recurring note here.\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(
        ctx, json.dumps({"terms": ["spend", "transaction", "recurring"]})
    )
    # both files surface
    assert "file_hi.md" in out and "file_lo.md" in out
    # file_hi (3 distinct terms) ranks above file_lo (1 term)
    assert out.index("file_hi.md") < out.index("file_lo.md")
    # line-numbered snippets present
    assert "L1:" in out and "L2:" in out
    # numeric parity with fs_read_file's comma formatting
    assert "174,897" in out


@pytest.mark.asyncio
async def test_fs_grep_is_case_insensitive(tmp_path):
    (tmp_path / "r.md").write_text("Total SPEND was high.\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(ctx, json.dumps({"terms": ["spend"]}))
    assert "r.md" in out


@pytest.mark.asyncio
async def test_fs_grep_no_matches_signal(tmp_path):
    (tmp_path / "r.md").write_text("nothing relevant here\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(ctx, json.dumps({"terms": ["bureau", "fico"]}))
    assert out.startswith("No matches for:")
    assert "bureau" in out and "fico" in out


@pytest.mark.asyncio
async def test_fs_grep_empty_terms(tmp_path):
    (tmp_path / "r.md").write_text("anything\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(ctx, json.dumps({"terms": []}))
    assert out == "No terms provided."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_tools/test_fs_tools.py -k fs_grep -v`
Expected: FAIL with `ImportError: cannot import name 'fs_grep'`.

- [ ] **Step 3: Implement `fs_grep`**

Add module constants near the top of `tools/fs_tools.py` (after `_LONG_NUMERIC_RE`, ~line 24):

```python
_MAX_SNIPPETS_PER_FILE = 5
_MAX_SNIPPET_LEN = 200


def _truncate(line: str) -> str:
    line = line.rstrip("\n")
    return line if len(line) <= _MAX_SNIPPET_LEN else line[:_MAX_SNIPPET_LEN] + "…"
```

Add the tool after `fs_read_file` (end of file):

```python
@function_tool
async def fs_grep(ctx: RunContextWrapper[AppContext], terms: list[str]) -> str:
    """Search case-folder file CONTENTS for any of `terms` (case-insensitive OR).

    Returns files ranked by how many distinct terms matched, each with a few
    `L<n>: <line>` snippets (1-based line numbers that line up with
    fs_read_file's start_line/end_line). Discovery only — read the file (or a
    slice of it) before drafting.
    """
    folder = ctx.context.case_folder
    if folder is None or not folder.exists():
        return "No case folder available."
    cleaned = [t for t in (terms or []) if t and t.strip()]
    if not cleaned:
        return "No terms provided."
    patterns = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in cleaned]

    results = []  # (name, matched_terms:set, matching_lines:int, snippets:list)
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        try:
            text = _format_long_numerics(p.read_text())
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable file must not abort discovery
        matched_terms = set()
        matching_lines = 0
        snippets = []  # (lineno, line)
        for i, line in enumerate(text.splitlines(), start=1):
            hit_terms = [t for (t, pat) in patterns if pat.search(line)]
            if hit_terms:
                matched_terms.update(hit_terms)
                matching_lines += 1
                if len(snippets) < _MAX_SNIPPETS_PER_FILE:
                    snippets.append((i, line))
        if matched_terms:
            results.append((p.name, matched_terms, matching_lines, snippets))

    if not results:
        return "No matches for: [" + ", ".join(cleaned) + "]"

    # rank: more distinct terms first, then more matching lines, then name
    results.sort(key=lambda r: (-len(r[1]), -r[2], r[0]))

    blocks = []
    for name, terms_set, n_lines, snippets in results:
        matched = ", ".join(sorted(terms_set))
        header = f"{name}  (matched: {matched} | {n_lines} hits)"
        body = "\n".join(f"  L{n}: {_truncate(line)}" for n, line in snippets)
        blocks.append(header + ("\n" + body if body else ""))
    return "\n".join(blocks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_tools/test_fs_tools.py -k fs_grep -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Stage & commit (only with user go-ahead)**

```bash
git add tools/fs_tools.py tests/test_tools/test_fs_tools.py
git commit -m "feat(report): add fs_grep content-search tool for consolidated reports"
```

---

### Task 2: ranged `fs_read_file`

**Files:**
- Modify: `tools/fs_tools.py:60-75` (`fs_read_file`)
- Test: `tests/test_tools/test_fs_tools.py`

**Interfaces:**
- Consumes: `_format_long_numerics` (existing).
- Produces: extended `fs_read_file(ctx, filename: str, start_line: int = 0, end_line: int = 0) -> str`. `0,0` → whole file (unchanged). A range returns the inclusive 1-based window after numeric formatting; out-of-range / inverted bounds clamp to the file extent. Confinement and "File not found" handling unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools/test_fs_tools.py`:

```python
_FIVE_LINES = (
    "line one alpha\n"
    "line two beta\n"
    "line three gamma\n"
    "line four delta\n"
    "line five epsilon\n"
)


@pytest.mark.asyncio
async def test_fs_read_file_whole_file_default_unchanged(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(ctx, json.dumps({"filename": "r.md"}))
    assert "alpha" in out and "epsilon" in out  # all lines present


@pytest.mark.asyncio
async def test_fs_read_file_range_returns_window(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "r.md", "start_line": 2, "end_line": 4})
    )
    assert "beta" in out and "gamma" in out and "delta" in out
    assert "alpha" not in out and "epsilon" not in out


@pytest.mark.asyncio
async def test_fs_read_file_range_clamps_out_of_bounds(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "r.md", "start_line": 4, "end_line": 999})
    )
    assert "delta" in out and "epsilon" in out
    assert "gamma" not in out


@pytest.mark.asyncio
async def test_fs_read_file_range_inverted_bounds_swap(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "r.md", "start_line": 4, "end_line": 2})
    )
    assert "beta" in out and "gamma" in out and "delta" in out
    assert "alpha" not in out and "epsilon" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_tools/test_fs_tools.py -k "range or whole_file" -v`
Expected: the 3 range tests FAIL (unexpected keyword `start_line`); `whole_file_default` PASSES.

- [ ] **Step 3: Implement the range**

Replace the body of `fs_read_file` (`tools/fs_tools.py:60-75`) with:

```python
@function_tool
async def fs_read_file(
    ctx: RunContextWrapper[AppContext],
    filename: str,
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    folder = ctx.context.case_folder
    if folder is None:
        return "No case folder available."
    target = (folder / filename).resolve()
    # Confine to case_folder to prevent path traversal.
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return f"Access denied: '{filename}' is outside the case folder."
    if not target.exists() or not target.is_file():
        return f"File not found: {filename}"
    # Comma-format long numeric runs so they survive boundary redaction.
    formatted = _format_long_numerics(target.read_text())
    # Default (0, 0) → whole file, unchanged.
    if start_line <= 0 and end_line <= 0:
        return formatted
    # Ranged read: 1-based inclusive, clamped to the file extent.
    lines = formatted.splitlines()
    n = len(lines)
    lo = start_line if start_line > 0 else 1
    hi = end_line if end_line > 0 else n
    lo = max(1, min(lo, n))
    hi = max(1, min(hi, n))
    if lo > hi:
        lo, hi = hi, lo
    return "\n".join(lines[lo - 1 : hi])
```

- [ ] **Step 4: Run the full fs_tools suite to verify it passes**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_tools/test_fs_tools.py -v`
Expected: PASS (all — original 3 + Task 1's 4 + Task 2's 4).

- [ ] **Step 5: Stage & commit (only with user go-ahead)**

```bash
git add tools/fs_tools.py tests/test_tools/test_fs_tools.py
git commit -m "feat(report): add optional line-range to fs_read_file for slice reads"
```

---

### Task 3: rewrite `report_needle.md` with path selection + grep path

**Files:**
- Modify: `skills/workflow/report_needle.md` (full rewrite of body; frontmatter unchanged)
- Test: `tests/test_skills/test_report_needle.py` (new)

**Interfaces:**
- Consumes: `skills.loader.load_skill(path) -> Skill` (`.body`, `.mode`, `.meta`).
- Produces: an updated inline skill body that the report agent inlines verbatim. No code interface; the test guards that the file still parses and carries the new guidance.

- [ ] **Step 1: Write the failing test**

Create `tests/test_skills/test_report_needle.py`:

```python
"""Guards that the report_needle skill still parses and carries the grep path."""
from pathlib import Path

from skills.loader import load_skill

_SKILL = Path(__file__).parent.parent.parent / "skills" / "workflow" / "report_needle.md"


def test_report_needle_parses_and_stays_inline():
    skill = load_skill(_SKILL)
    assert skill.mode == "inline"
    # frontmatter inputs/outputs preserved in meta
    assert "question" in skill.meta.get("inputs", {})
    assert "coverage" in skill.meta.get("outputs", {})


def test_report_needle_documents_grep_path():
    body = load_skill(_SKILL).body
    # the new consolidated-file path is described
    assert "fs_grep" in body
    assert "start_line" in body and "end_line" in body
    # table routing is still present (multi-file primary path)
    assert "Concept → file" in body
    # grep is discovery-only
    assert "DISCOVERY ONLY" in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_skills/test_report_needle.py -v`
Expected: `test_report_needle_documents_grep_path` FAILS (`fs_grep` not in body); the parse test PASSES.

- [ ] **Step 3: Rewrite `skills/workflow/report_needle.md`**

Replace the entire file with:

```markdown
---
name: Report Needle
description: Pick relevant curated report files for a reviewer question; judge coverage
type: workflow
owner: [report_agent]
mode: inline
inputs:
  question: str
  available_files: list[str]
outputs:
  relevant_files: list[str]
  coverage: "explicit|implicit|not_mentioned"
  hints: list[str]
---

Find the report content that bears on the answer, then judge coverage honestly. The taxonomy is INTENTIONALLY narrow at the top end — only `explicit` when the report literally states the answer.

## Pick the path first

Look at the available files:

- **Canonical multi-file layout** (several `<domain>_exp_0.md` files): route by topic using the **Concept → file** table below. The file boundaries ARE the index — this is fast and preferred. Read the routed file(s) in full.
- **One long consolidated file** (a single big `.md`, or a few large files the table can't discriminate): the table gives no signal. Use `fs_grep` to find the relevant section inside the file, then read just that slice with `fs_read_file(filename, start_line, end_line)`.

When unsure which layout you're in, prefer the table; fall back to `fs_grep` only when routing yields nothing or there is effectively one file.

## Grep path (consolidated / long file)

1. Expand the (often vaguely-worded) question into a handful of candidate search terms + synonyms. Seed them from the Concept → file table's concept labels — e.g. a "spending pattern" question → `["spend", "spending", "transaction", "recurring", "merchant", "outlier"]`.
2. Call `fs_grep(terms=[...])`. It returns matching files ranked by how many of your distinct terms hit, each with `L<n>:` line-numbered snippets.
3. Read a window around the top matches: `fs_read_file(filename="<file>", start_line=<n-5>, end_line=<n+30>)`. Widen the window if the section is larger. NEVER read the whole long file.
4. Judge coverage from the slice. `not_mentioned` on this path means `fs_grep` returned `No matches for: [...]` after a reasonable term expansion.

## Coverage

- `explicit` — the report directly states the answer (or the specific facts the question asks for, in a form a reader could quote without inference). Don't use this just because the report touches on the topic.
- `implicit` — the report contains relevant facts but the answer requires INFERENCE / SYNTHESIS from those facts. Topic is covered; specific answer is not. This is the default when the report only frames or partially addresses the question.
- `not_mentioned` — the report doesn't cover the question's topic (folder empty, no listed file plausibly addresses it, or `fs_grep` found nothing across contents).

Bias: when in doubt, prefer `implicit` over `explicit`. Most curated reports give context, not direct answers — claiming `explicit` when the answer is being inferred over-states the report and crowds out specialist data in downstream balancing. Use `explicit` ONLY when you can quote a verbatim line from the report that IS the answer.

## Concept → file (canonical `<domain>_exp_0.md` layout)

| Reviewer concept | File |
|---|---|
| FICO / bureau score / external tradelines / external delinquency | `bureau_exp_0.md` |
| default journey / DPD progression / DPD timeline | `default_journey_exp_0.md` |
| **cards (count/balance/limit) / consumer/commercial card / portfolio mix / merchant relationships** | **`crossbu_exp_0.md`** |
| score drivers / risk factors | `driver_exp_0.md` |
| summary / overview / headline / broad multi-domain | `executive_summary_exp_0.md` |
| notable findings / what's interesting | `interestingness_exp_0.md` |
| internal model score / PD / GAM | `modeling_exp_0.md` |
| payments / payment returns / spend / spend spikes | `payment_spend_exp_0.md` |
| **spending pattern / spend behavior / spend trajectory / pattern questions / what's atypical / merchant exposure / recurring transactions / high-value outliers / late-stage spends** | **`payment_spend_exp_0.md` AND `interestingness_exp_0.md`** (BOTH — payment_spend has the dataset overview, interestingness has the structurally-atypical points across temporal / merchant / persistence dimensions) |
| recommended action / next step / treatment | `strategy_0.md` |
| WCC / write-off / collections | `wcc_notes_exp_0.md` |

Rules:
- Unambiguous routing match → pick the file. Coverage is `explicit` ONLY if the file directly states the answer; otherwise `implicit`.
- When in doubt, over-include — reading one extra file is cheap. Add `executive_summary_exp_0.md` as fallback when uncertain.
- NEVER return `not_mentioned` because literal keywords don't appear in filenames; translate via the table first, and on a consolidated file use `fs_grep` before concluding. `not_mentioned` is only when the folder is empty, OR routing + filename + domain reasoning all yield nothing, OR `fs_grep` returns no content matches.
- Filenames may not match the canonical layout — fall back to topic-hint matching against filenames, then `fs_grep`.
- **Pattern / trajectory / "what's atypical" framings** are inherently multi-aspect — pull `interestingness_*` alongside the topic-domain file (e.g. `payment_spend_exp_0.md` + `interestingness_exp_0.md` for spending pattern). The interestingness report carries the cross-cutting structural observations that no single domain file contains.
- `fs_grep` is DISCOVERY ONLY — it never replaces reading. On the grep path you still read a slice before drafting; on the multi-file path you read the routed file in full.

Output:
```json
{ "relevant_files": ["..."], "coverage": "explicit|implicit|not_mentioned", "hints": ["one per file"] }
```
`hints` length == `relevant_files` length. If `coverage == "not_mentioned"`, both empty.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_skills/test_report_needle.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Stage & commit (only with user go-ahead)**

```bash
git add skills/workflow/report_needle.md tests/test_skills/test_report_needle.py
git commit -m "feat(report): report_needle picks grep path for consolidated single-file reports"
```

---

### Task 4: wire `fs_grep` into the report agent

**Files:**
- Modify: `agent_factories/report_agent.py:18` (import), `:42` (tool-count line), `:49-59` (workflow steps), `:91` (tools list)
- Test: `tests/test_agent_factories/test_report_agent_tools.py` (new)

**Interfaces:**
- Consumes: `fs_grep`, `fs_list_files`, `fs_read_file` from `tools.fs_tools`; `build_report_agent(model) -> Agent`.
- Produces: `build_report_agent` returns an `Agent` whose `tools` include `fs_grep`, `fs_list_files`, `fs_read_file` (by `.name`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_factories/test_report_agent_tools.py`:

```python
"""The report agent is wired with fs_grep alongside the existing fs tools."""
from agent_factories.report_agent import build_report_agent


def test_report_agent_exposes_fs_grep():
    agent = build_report_agent(model="gpt-4.1")
    names = {t.name for t in agent.tools}
    assert {"fs_grep", "fs_list_files", "fs_read_file"} <= names
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_agent_factories/test_report_agent_tools.py -v`
Expected: FAIL — `fs_grep` not in the tool set.

- [ ] **Step 3: Wire the tool and update the agent's workflow prose**

In `agent_factories/report_agent.py`:

(a) Update the import (line 18):

```python
from tools.fs_tools import fs_grep, fs_list_files, fs_read_file
```

(b) Update the tool-count sentence (line 42):

```python
You have three tools: fs_list_files, fs_grep, fs_read_file.
```

(c) Replace the `Workflow:` block (lines 49-59) with:

```python
Workflow:
1. Your input includes a file list. First decide the layout:
   - **Several curated `<domain>_exp_0.md` files** → use the Coverage rubric's
     **Concept → file** table to pick the 1-2 most relevant files, then call
     `fs_read_file(filename="<chosen_file>")` on them (batch in ONE round, ≤ 2 files).
   - **One long consolidated file** (or files the table can't discriminate) →
     expand the question into search terms and call `fs_grep(terms=[...])`, then
     read a slice around the top matches with
     `fs_read_file(filename="<file>", start_line=<n>, end_line=<m>)`.
   The filenames in the list are ARGUMENTS to the read tools, NOT tool names.
2. Emit ReportDraft immediately from what you read — in the SAME turn as the
   read result arrives. Do NOT read more files "for context," do NOT
   deliberate, do NOT re-derive or recompute anything from the report. Copy
   the load-bearing lines into your bullets/excerpts and stop.
```

(d) Update the tools list (line 91):

```python
        tools=[fs_grep, fs_list_files, fs_read_file],
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_agent_factories/test_report_agent_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full affected suites**

Run: `cd "/Users/mingxuanliu/Library/CloudStorage/GoogleDrive-mingxuan99michelle@gmail.com/My Drive/Projs/AgenticSys_v2" && python -m pytest tests/test_tools/test_fs_tools.py tests/test_skills/test_report_needle.py tests/test_agent_factories/test_report_agent_tools.py -v`
Expected: PASS (all).

- [ ] **Step 6: Stage & commit (only with user go-ahead)**

```bash
git add agent_factories/report_agent.py tests/test_agent_factories/test_report_agent_tools.py
git commit -m "feat(report): wire fs_grep into report agent and document path selection"
```

---

## Self-Review

**Spec coverage:**
- `fs_grep` tool (engine, ranking, line numbers, no-match/empty/no-folder, numeric parity, confinement, per-file error skip) → Task 1. ✓
- Ranged `fs_read_file` (default whole-file, inclusive 1-based window, clamping, inversion) → Task 2. ✓
- `report_needle.md` path selection + grep path + tightened `not_mentioned` + table kept primary + discovery-only → Task 3. ✓
- Wiring into `report_agent.py` (import, tools list, workflow prose) → Task 4. ✓
- Tests for all of the above → Tasks 1–4. ✓

**Placeholder scan:** none — every code/test step shows complete content.

**Type consistency:** `fs_grep(ctx, terms: list[str]) -> str` and `fs_read_file(ctx, filename, start_line=0, end_line=0) -> str` used identically across tasks/tests; tool `.name` values (`fs_grep`/`fs_list_files`/`fs_read_file`) match the wiring assertion in Task 4.

**Note:** Tests live in the existing `tests/test_tools/test_fs_tools.py` (extended) rather than a standalone file — this supersedes the spec's earlier `tests/test_fs_grep.py` placeholder.
```
