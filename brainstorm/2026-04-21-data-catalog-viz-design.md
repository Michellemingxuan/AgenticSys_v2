# Data Catalog Visualization — Design

**Date:** 2026-04-21
**Output file:** `brainstorm/data_catalog.html`
**Style reference:** `architecture-v7.html` — Amex navy/blue, Helvetica, cards with colored left borders, tight uppercase labels.
**First principle:** Visual clarity. Skip clutter; prefer whitespace and strong hierarchy.

## Scope

Browse-oriented catalog (option A) with features:
- (i) Search box filtering tables and columns
- (ii) Expand/collapse columns per table
- (iii) Sidebar nav listing all tables
- (vi) Summary header with totals

Explicitly excluded: inline column metadata chips (range/values), correlations section, per-column distribution mini-charts.

## Data source

All 11 YAML files in `config/data_profiles/`:
`bureau`, `cross_bu`, `cust_tenure`, `income_dti`, `model_scores`, `payments`, `score_drivers`, `spends`, `txn_monthly`, `wcc_flags`, `xbu_summary`.

Parsed ahead of time and embedded as a JSON blob inside the HTML's `<script>` tag, so the file is standalone and opens from the filesystem without a server.

## Layout

Top to bottom, then left-to-right:

### 1. Hero header
- Amex navy background, full-width, blue top stripe (matches `architecture-v7.html`).
- Title: **Data Catalog**
- Subtitle (small uppercase, letter-spaced): e.g., `AGENTIC CASE REVIEW · SIMULATED DATA PROFILES`
- Stats strip: three large numbers separated by dots —
  **N tables · N columns · N correlations** (computed from YAMLs at build time).

### 2. Two-column body

**Left sidebar** (~240px, sticky on scroll):
- Search input at top — filters both sidebar list and main panel live.
- Expand-all / Collapse-all small text buttons below search.
- Vertical list of all table names.
- Clicking a name smooth-scrolls the main panel to that table's card and highlights the sidebar row.
- IntersectionObserver auto-highlights the sidebar row whose card is currently in view.

**Main panel** — vertical stack of table cards, full-width (no grid), one card per table.

## Table card

### Collapsed
- Blue colored left border (`.alc` pattern from `architecture-v7.html`).
- Row 1: table name (large, navy, monospace or bold Helvetica) + right-aligned pill `N COLUMNS`.
- Row 2: description (gray body text, 1–2 lines).
- Chevron on the far right indicating expandable.
- Entire card is clickable to toggle.

### Expanded
- Reveals a clean three-column list of columns:
  - **Column name** (monospace, navy)
  - **Type** (small uppercase tag: `INT`, `FLOAT`, `STRING`, `DATE`, `CATEGORICAL`)
  - **Description** (gray body text)
- `case_id` is included (not hidden like in `to_prompt_context`) — user may want to see the foreign-key column.
- No range chips, no category value chips, no distribution info — deliberately minimal.

## Interactions

- Smooth scrolling on sidebar clicks.
- First card starts expanded; the rest collapsed — keeps initial page short.
- Expand-all / Collapse-all toggle every card at once.
- Search matches on: table name, table description, column name, column description.
- Tables with zero matches hide entirely while a search term is active.
- Sidebar list hides non-matching tables in sync.
- "No matches" message in main panel when nothing matches.

## Visual tokens (reuse from architecture-v7.html)

```
--navy: #00175A
--blue: #006FCF
--gray-light: #F4F6F9
--gray-text: #4A5568
--gray-border: #E2E8F0
```
Type tags use the existing chip pattern (small, 10px, uppercase, rounded 2px).
Card border-left 3px blue; hover lifts shadow slightly.

## File structure

Single `data_catalog.html`:
- `<style>` block inline
- `<script>` block with:
  - `const CATALOG = [...]` — embedded JSON parsed from the 11 YAMLs
  - render logic (build sidebar, build cards)
  - search handler, expand/collapse handlers, IntersectionObserver

No external JS/CSS dependencies. No build step.
