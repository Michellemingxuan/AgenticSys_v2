# Design: `fs_grep` within-file content search for the report agent

**Date:** 2026-06-25
**Topic:** Equip the report-needle skill with a content-search ("grep") tool for the
consolidated / long single-file report case, where Concept → File routing can't help.
**Status:** Approved design, pending spec review.

## Problem

The report agent (`agent_factories/report_agent.py`) selects which curated report
file(s) to read using a **static Concept → File mapping table** in
`skills/workflow/report_needle.md`. File selection is pure reasoning over
filenames plus that table — nothing searches *inside* file contents.

For the **canonical multi-file layout** (`<domain>_exp_0.md` set) this works well
and is fast: the file boundaries *are* the index, and the agent reaches the right
1–2 files in ~2 model round-trips. We keep it.

It breaks down for the **consolidated long-file case** — when a case's report is
wrapped into a single long `.md` file (or a small number of large files):

- Routing can't discriminate: there's only one file, so the table gives no signal.
- Reading the whole long file is slow *and* dilutes the evidence with large
  irrelevant sections, hurting excerpt quality.

For that case the agent needs to search *within* the file to find the relevant
section, then read only that region.

## Decisions (locked during brainstorming)

1. **Grep's role:** *within-file locator for the consolidated / long-file case* —
   NOT a universal primary mechanism. The **Concept → File table stays primary**
   for the canonical multi-file layout. The agent chooses the path from what
   `fs_list_files` shows: a canonical multi-file set → route via the table; one
   long file (or few large files routing can't discriminate) → grep.
2. **Matching engine:** stdlib `re`, case-insensitive, **multi-term OR**. The agent
   expands the vague question into candidate terms/synonyms and passes them as a
   list; the tool ORs them. Vagueness is absorbed by the agent's term expansion,
   not by fuzzy/semantic matching. No new dependencies; no embedding
   infrastructure (the repo has none).
3. **Read depth (mode-dependent):**
   - **Multi-file (table) path:** unchanged — read the routed file(s) in full,
     then draft.
   - **Long-file (grep) path:** grep returns matching **line numbers**; the agent
     reads a **slice around the matches** via an extended `fs_read_file`
     (optional line range), then drafts. This is where the speed + evidence-focus
     win comes from — the whole long file is never ingested.

## Architecture

### Path selection

```
fs_list_files()
  ├─ canonical multi-file set ──> [table routing] ─> fs_read_file(file)        ─> ReportDraft
  └─ one long file / few large ─> [expand terms] ─> fs_grep(terms)
                                       └─> ranked filename:LINE matches
                                             └─> fs_read_file(file, start, end)  ─> ReportDraft
                                                   (slice around matches)
```

The Concept → File table also seeds good grep terms even on the grep path.

### Component 1 — `fs_grep` tool

Location: `tools/fs_tools.py` (alongside `fs_list_files` / `fs_read_file`).

Signature:

```python
@function_tool
async def fs_grep(ctx: RunContextWrapper[AppContext], terms: list[str]) -> str:
    ...
```

Behavior:

- **Confinement:** operates only over files in `ctx.context.case_folder` (same
  boundary discipline as the existing fs tools; it iterates files in the folder).
  Returns the standard "No case folder available." string when the folder is missing.
- **Numeric consistency:** reads each file through the **same
  `_format_long_numerics`** pass `fs_read_file` uses, so snippet text — and the
  line numbering — matches exactly what the agent will later slice-read and quote.
- **Matching:** for each line, case-insensitive substring match against **any**
  term using `re.escape(term)` + `re.IGNORECASE` (OR semantics). Substring (not
  word-boundary) matching, to stay forgiving of stems/variants.
- **Empty input:** an empty `terms` list returns `"No terms provided."` rather than
  matching everything.
- **Output — line-numbered, ranked:** group matches by file, rank by *number of
  distinct terms matched* then total hit count. For each file emit the filename,
  matched-term set, total hit count, and up to ~5 snippet lines as **`L<n>: <text>`
  using 1-based line numbers** (long lines truncated). The line numbers are the
  contract the agent feeds back into `fs_read_file`'s range. Example:

  ```
  full_case_report.md  (matched: spend, recurring | 9 hits)
    L142: Recurring spend at MERCHANT_A held steady at $4,200/mo...
    L160: A high-value outlier of $174,897.36 posted on...
  ```

- **No matches:** return `"No matches for: [term, ...]"` → drives `not_mentioned`.

### Component 2 — `fs_read_file` extended with an optional line range

Backward-compatible extension in `tools/fs_tools.py`:

```python
@function_tool
async def fs_read_file(
    ctx: RunContextWrapper[AppContext],
    filename: str,
    start_line: int = 0,
    end_line: int = 0,
) -> str:
    ...
```

- Sentinel defaults `0` (= unset) match the repo's strict-schema-friendly
  convention (`top_n: int = 10`, `filter_value: str = ""`); we avoid
  `int | None` so the tool stays valid under the SDK's default strict mode.
- Default (`0`, `0`) → current behavior: whole file, unchanged.
- With a range → return only lines `[start_line, end_line]` (1-based, inclusive),
  after the `_format_long_numerics` pass, so line numbers align with `fs_grep`
  output. Out-of-range / inverted bounds are clamped to the file extent.
- Confinement and "File not found" handling unchanged.

### Component 3 — `report_needle.md` rewrite

The skill stays `mode: inline`. Body changes:

- **Path-selection heuristic up front:** inspect the available files; canonical
  multi-file set → table routing (existing flow, full read). One long file / few
  large files → grep path.
- **Table routing kept as the primary, documented multi-file flow** (current
  content largely retained), now explicitly the path for canonical layouts.
- **New grep path:** expand the vague question into candidate terms (seeded by the
  table's concept labels), call `fs_grep(terms)`, then `fs_read_file(file,
  start_line, end_line)` for a window around the top matches, then judge coverage.
- **Coverage semantics unchanged** (`explicit` / `implicit` / `not_mentioned`,
  bias toward `implicit`). On the grep path, `not_mentioned` is anchored to "grep
  found nothing across contents after a reasonable term expansion."
- Document the `fs_grep(terms)` and ranged `fs_read_file` affordances.

### Component 4 — Wiring (`agent_factories/report_agent.py`)

- Import `fs_grep` from `tools.fs_tools`.
- `tools=[fs_grep, fs_list_files, fs_read_file]`.
- No other change: instructions update automatically because the
  `report_needle.md` body is inlined into `REPORT_AGENT_INSTRUCTIONS`. The
  `ReportDraft` output contract is unchanged.

## Performance note

This design is **latency-neutral for the common multi-file case** (table path is
untouched — no extra round-trip) and a **net win for the long-file case** (grep +
slice read avoids ingesting a large file and replaces an otherwise-huge read,
rather than adding to it). The earlier "grep as universal primary" idea was
rejected precisely because it added a discovery round-trip to every question.

## Error handling

- Missing/absent case folder → `"No case folder available."` (parity with fs tools).
- Empty `terms` → `"No terms provided."`.
- No content matches → `"No matches for: [...]"` → agent judges `not_mentioned`.
- Per-file read errors during the grep sweep are skipped for that file; the sweep
  continues (one unreadable file must not abort discovery).
- `fs_read_file` range out of bounds / inverted → clamped to file extent.

## Testing

New `tests/test_tools/test_fs_tools.py` (following `tests/test_*.py` conventions):

- **OR matching:** a file matches when it contains *any* term.
- **Case-insensitivity:** `SPEND` matches `spend`.
- **Ranking:** more distinct terms matched ranks higher.
- **Line numbers:** output `L<n>:` refs are 1-based and align with the file; a
  range read of those numbers returns the matched lines.
- **No-match / empty-terms signals:** correct guidance strings.
- **Confinement:** only case-folder files searched; missing folder → standard string.
- **Numeric consistency:** 6+ digit values appear comma-formatted in snippets,
  identical to `fs_read_file`.

Extend `fs_read_file` tests (or add to the same module):

- **Whole-file default unchanged** (regression).
- **Range read:** returns exactly the requested inclusive 1-based window.
- **Clamping:** out-of-range / inverted bounds clamp to the file extent.

## Out of scope / YAGNI

- Grep as a universal primary discovery mechanism for the multi-file case —
  rejected (adds a round-trip for no gain where the table already routes well).
- Fuzzy (`difflib`) and semantic/embedding matching — rejected; agent-side term
  expansion covers vagueness without new infrastructure.
- Shelling out to the Unix `grep` binary — rejected; breaks the Python-confined
  `case_folder` boundary.
- Drafting from grep snippets without any read — rejected; the long-file path uses
  a bounded slice read so evidence keeps surrounding context.
- Configurable snippet/term/window caps — fixed sensible defaults; revisit if needed.

## Files touched

- `tools/fs_tools.py` — add `fs_grep`; extend `fs_read_file` with optional line range.
- `skills/workflow/report_needle.md` — add the grep path; keep table routing primary.
- `agent_factories/report_agent.py` — wire `fs_grep` into the tools list.
- `tests/test_tools/test_fs_tools.py` — new tests (grep + ranged read).
