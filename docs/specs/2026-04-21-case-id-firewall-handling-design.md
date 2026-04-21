# Case-ID Firewall Handling — Design

**Date:** 2026-04-21
**Status:** Draft · awaiting review

## Problem

Each case's data is stored as a folder named with its case ID:

```
data/simulated/CASE-00001/
  bureau.csv
  payments.csv
  … (11 tables)
```

The content firewall (see [gateway/firewall_stack.py](../../gateway/firewall_stack.py) and [brainstorm/firewall_overview.html](../../brainstorm/firewall_overview.html)) masks 8+-digit runs on every call and 6+ on retry, plus filters role-injection tokens and code-exec keywords. Raw case-ID strings (`CASE-00001`) and absolute folder paths leaking into LLM-bound content create two risks:

- **A. Case-ID token leak** — the case ID itself should never appear in any LLM prompt or tool result. Specialists reason about "the current case," not a specific ID.
- **B. Path leak** — if tool-result or error strings include `data/simulated/CASE-00001/…` fragments, the case-ID token flows to the LLM.

## Current leak surface

| Layer                              | Leaks case_id?          | Notes                                                                                   |
| ---------------------------------- | ----------------------- | --------------------------------------------------------------------------------------- |
| Per-case CSVs on disk              | No                      | Generator strips `case_id` column at write time ([data/generator.py:329](../../data/generator.py#L329)). |
| Gateway row-load path              | No                      | Strips `case_id` on pivot ([data/gateway.py:117](../../data/gateway.py#L117)).            |
| Catalog `to_prompt_context()`      | No (defensive filter)   | Filters `case_id` at [data/catalog.py:94-95](../../data/catalog.py#L94-L95) — becomes dead code once profiles cleaned. |
| Catalog `get_schema()`             | **Yes**                 | No filter; returns `case_id` to tool callers of `get_table_schema()`.                  |
| Data profiles (11 YAMLs)           | **Yes (upstream)**      | All declare `case_id: CASE-{seq:05d}`; source of the leak above.                        |
| `list_available_tables()` tool     | **Yes**                 | Returns `"Tables for case CASE-00001:"` ([tools/data_tools.py:37](../../tools/data_tools.py#L37)).         |
| Agent / specialist prompts         | No                      | Sweep of [agents/](../../agents/) found no references.                                    |
| Orchestrator prompt builder        | No                      | Already filters `case_id` at [orchestrator/team.py:105](../../orchestrator/team.py#L105). |
| Gateway/tool error messages        | Possible                | Error paths not yet normalized; could surface raw case-ID tokens.                       |

## Design

Four surgical changes plus a defense-in-depth sanitizer. Matches the firewall's existing two-layer strategy (prevent + scrub).

### 1. Profile layer — remove `case_id` from per-table YAMLs

**Change:** delete the `case_id` column block from all 11 files under [config/data_profiles/](../../config/data_profiles/).

**Move to generator framework:** [data/generator.py](../../data/generator.py) gains module-level constants:

```python
CASE_ID_COLUMN = "case_id"
CASE_ID_FORMAT = "CASE-{seq:05d}"
```

The generator's existing per-case partitioning logic (lines 297-329) injects the case-ID column itself rather than reading it from each profile. Every table gets it identically — it's infrastructure, not table schema.

**New config file:** `config/generation.yaml` as the single source of truth for generation parameters. Initial contents:

```yaml
n_cases: 50   # matches the current code default in data/__main__.py
```

Replaces the implicit coupling where `case_id`'s row count controlled the generation size.

**Catalog impact:** With `case_id` gone from profiles, `get_schema()` naturally returns only real data columns. One small cleanup — [data/catalog.py:94-95](../../data/catalog.py#L94-L95) currently carries a defensive `if col == "case_id": continue` in `to_prompt_context()`; that line becomes dead code and should be removed so the catalog stops silently hiding a column it no longer knows about.

### 2. Tool output layer — neutral case phrasing

**Change** [tools/data_tools.py](../../tools/data_tools.py):

```python
# Before
if _gateway is not None:
    case_id = _gateway.get_case_id()
    if case_id:
        case_tables = _gateway.list_tables()
        header = f"Tables for case {case_id}:\n"
        return header + "\n".join(case_tables) if case_tables else header + "No tables available"

# After
if _gateway is not None:
    case_tables = _gateway.list_tables()
    if case_tables:
        return "Tables for the current case:\n" + "\n".join(case_tables)
    return "No tables available for the current case."
```

Grep confirmed `list_available_tables()` is the only tool surfacing the raw case ID to the LLM. `_gateway.get_case_id()` remains available to internal callers (orchestrator session state, event logger).

### 3. Gateway + path handling — relative tokens in LLM-bound strings

**Change** [data/gateway.py](../../data/gateway.py):

Add a helper that renders user/tool-facing path strings with a neutral case token:

```python
def _display_path(self, table: str) -> str:
    return f"<case>/{table}.csv"
```

All externally-surfaced error messages (e.g., `"Data unavailable: table not found at {path}"`) use `_display_path()` instead of the absolute filesystem path.

**Internal uses keep the real path.** Only strings that can flow to callers / tool results / LLM context are scrubbed.

**Docstring warning on `get_case_id()`:**

> Return value MUST NOT be passed into any LLM-bound string. Use `<case>` token instead.

No changes to `set_case()`, `list_case_ids()`, `query()`, or row-level `case_id` stripping.

### 4. LLM-boundary scrubber — defense in depth

**New module:** `gateway/case_scrubber.py`. Intentionally small scope — one job, mask `CASE-\d+` tokens:

```python
import re

_CASE_TOKEN = re.compile(r"\bCASE-\d+\b", flags=re.IGNORECASE)

def scrub(text: str) -> str:
    """Replace CASE-\\d+ tokens with <case>. Idempotent."""
    return _CASE_TOKEN.sub("<case>", text)
```

**Integration point:** invoked inside [SafeChainAdapter](../../gateway/safechain_adapter.py) during pre-sanitization, alongside the existing 8+-digit masker. Runs on every LLM call.

**Rationale for placement:**
- `FirewallStack` is platform-agnostic retry logic — keeps scrubbing out of there.
- `SafeChainAdapter` is where pre-sanitization already lives (per existing [firewall_overview.html](../../brainstorm/firewall_overview.html) documentation). Case scrubbing is adjacent to the digit mask.
- `OpenAIAdapter` is the "dev bypass — no firewall simulation" path per existing convention, so the scrubber is not invoked there. Local dev logs may still contain raw case IDs, which is acceptable.

## Non-changes (explicit)

- Event logger, `step_history`, and internal session state keep the **raw case ID** — needed for debugging and audit trails. Only LLM-bound content is scrubbed.
- No change to row-level `case_id` handling in the gateway (already correct).
- No change to orchestrator prompt builder (already compliant).
- No change to CLI argument handling in `main.py` (internal only).

## Testing

1. **Generator / profiles:** regenerate all 11 CSVs from cleaned YAMLs + new `config/generation.yaml`; assert per-case folders still contain 11 tables with identical data-column schemas. Extend [tests/test_data/test_generator.py](../../tests/test_data/test_generator.py) if needed.
2. **Tool outputs:** extend [tests/test_tools/test_data_tools.py](../../tests/test_tools/test_data_tools.py) to assert `list_available_tables()` returns `"Tables for the current case:"` and contains no substring matching `CASE-\d+`.
3. **Case scrubber:** new `tests/test_gateway/test_case_scrubber.py` covering:
   - Basic token replacement (`"see CASE-00001 payments"` → `"see <case> payments"`)
   - Case-insensitive match
   - Tokens embedded in JSON
   - Idempotence (`scrub(scrub(x)) == scrub(x)`)
   - No-op on empty / no-match strings
4. **Adapter integration:** integration test asserting that a specialist response containing a `CASE-\d+` token does not leak through a full `SafeChainAdapter.run()` round-trip.

## Risks

- **New tools that surface paths or schemas** need the same discipline. The adapter-level scrubber is a safety net, not a substitute for careful tool design. Add to the code review checklist.
- **Downstream consumers parsing schema JSON** that expected `case_id` as a column would break. Grep found none in the repo; call out in the implementation plan to re-verify before deleting from YAMLs.
- **Generator change ordering:** YAMLs can't be edited in isolation from the generator infrastructure update — both must land together or generation breaks.

## Out of scope

- User/email path scrubbing. Confirmed with user: real deployment paths contain only the case-ID token, not the Google Drive dev artifact. Google Drive paths are a local-dev concern, not a production firewall concern.
- Account-number / PII masking beyond what `FirewallStack` and `SafeChainAdapter` already handle (8+-digit runs, exec/eval/import filtering). This spec is scoped strictly to case-ID leakage.
- Multi-case simultaneous handling. System remains single-active-case per session.
