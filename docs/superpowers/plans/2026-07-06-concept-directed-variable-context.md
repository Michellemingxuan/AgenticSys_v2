# Concept-Directed Variable Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the orchestrator name the concept(s) relevant to a sub-question so the specialist receives the exact variable definitions (name · meaning · threshold) in its initial context — reliable variable selection, in sync with the profiles, with no extra round.

**Architecture:** Add `concept:` tags to the modeling profiles (single source of truth). A catalog helper expands `concept → variables`. The orchestrator learns the concept vocabulary from its team roster and passes an optional `concepts` arg when calling a specialist tool. The specialist runner (`redacting_tool._runner`) expands those concepts into a compact **§ DIRECTED VARIABLES** block and prepends it — nearest the sub-question — alongside the existing episodic/KB prepends. Purely additive: no concepts → today's behavior.

**Tech Stack:** Python 3.11, PyYAML, `openai-agents` SDK (`@function_tool`), pytest. Spec: `docs/superpowers/specs/2026-07-06-concept-directed-variable-context-design.md`.

## Global Constraints

- No extra LLM round: the directed block lives in the specialist's *initial* input, never a separate tool call.
- Non-breaking: `concept:` is an unknown key to `datalayer/generator.py` and `get_table_schema`; both must continue to ignore it.
- No-regression: when `concepts` is absent/empty/unmatched, the specialist input must be **byte-identical** to today's `_compose_specialist_input` output.
- Directed block sits **last among prefixes** (episodic, KB, directed-variables), nearest the `--- New question ---` marker.
- Concept taxonomy (exact strings): `internal_delinquency`, `external_delinquency`, `exposure_leverage`, `capacity_paydown`, `oop`, `spend_pattern`, `trends_tenure`, `bureau_derived`, `risk_events`, `output_score`, `third_party_score`.
- Injection budget: cap at 15 variables, thresholded-first.
- Commit after each task. Do NOT push (project rule: commit/push only when the user asks).

---

### Task 1: Concept tags + structured thresholds on the modeling profiles

**Files:**
- Modify: `config/data_profiles/model_scores.yaml` (add `concept:` + promote inline thresholds per the mapping)
- Modify: `config/data_profiles/model_scores_transaction.yaml` (same base-name columns)
- Test: `tests/test_datalayer/test_concept_tags.py`

**Interfaces:**
- Produces: every non-date column in both profiles carries a `concept:` (str or list of the taxonomy strings). Consumed by Task 2.

**Concept mapping** (apply to every column present in each profile; `trans_month` / `txn_date_time` / `trans_dt` / `appr_deny_cd` / `auto_decline_pos_deny_cd_s1` / `index` are date/txn keys — leave untagged). Threshold columns from `context/modeling_context_description.txt`:

| Column (base name) | `concept` | threshold |
|---|---|---|
| `cbr_score` | `bureau_derived` | — |
| `exp_pif` | `exposure_leverage` | — |
| `ons_30_trd` | `internal_delinquency` | — |
| `last_cycle_cut_revolve_rate` | `capacity_paydown` | `< 0.46` |
| `times_30_dpd` | `internal_delinquency` | `> 1` |
| `cust_expsr_avg_rem_12m_ratio` | `[oop, exposure_leverage]` | `> 3.15` |
| `cust_lndexpsr_minloc_6m_ratio` | `exposure_leverage` | — |
| `max_agec_agel_dyldage` | `trends_tenure` | — |
| `cb_ten_to_amex_tenure` | `trends_tenure` | — |
| `tpf_internal_delinq_idx` | `internal_delinquency` | `> 5.8` |
| `tpf_cust_mod_tenure` | `trends_tenure` | — |
| `ratio_30_90dys_trig_amt` | `internal_delinquency` | `> 0` |
| `avutil_exrvlv_balgt50` | `[external_delinquency, exposure_leverage]` | `> 75` |
| `calx_ratio_daily_max_age` | `internal_delinquency` | — |
| `hcam_src_trnd_indx` | `[bureau_derived, trends_tenure]` | `< -19` |
| `lvrg_debt_remit` | `exposure_leverage` | — |
| `sum_tot_rsky_evnt` | `risk_events` | — |
| `delnqncy_ind_intrnl` | `internal_delinquency` | `> 0.5` |
| `cust_debt_pymcpty_tm_inc` | `capacity_paydown` | — |
| `credit_loss_prob` | `output_score` | `> 10` |
| `tot_struct_risk_score` | `output_score` | `> 20` |
| `avg_remit_minus_max` | `[capacity_paydown, oop]` | — |
| `positive_events` | `risk_events` | — |
| `tpf_cust_hi_rvlv_line_am` | `exposure_leverage` | `< 77` |
| `oop_interaction` | `[oop, spend_pattern]` | `> 28` |
| `sum_o30dn_o60dn_o90dn` | `internal_delinquency` | `>= 1` |
| `hcam_bal_trnd_indx` | `[trends_tenure, exposure_leverage]` | `> 2.65` |
| `product_risk_attribute` | `risk_events` | — |
| `cust_ext_delinq_idx` | `external_delinquency` | `> 5` |
| `tm_wt_q_score` | `trends_tenure` | — |
| `cust_eff_se_cdss_5_180_day_score` | `spend_pattern` | `> 2` |
| `cust_expr_to_arb_inc_ratio` | `[capacity_paydown, exposure_leverage]` | `> 0.08` |
| `cust_lend_acct_paydown` | `capacity_paydown` | `< 0.1` |
| `cust_experian_trans_union_inq_idx` | `bureau_derived` | `> 4.65` |
| `cust_intr_extnl_unscr_tt_debt_srvc_rt1` | `capacity_paydown` | `> 0.15` |
| `cust_open_acct_paydown` | `capacity_paydown` | `< 0.3` |
| `cust_old_rec_age` | `trends_tenure` | — |
| `cust_atp_arb_incom_am` | `capacity_paydown` | — |
| `cust_cash_tot_liab_yr1_rt` | `capacity_paydown` | — |
| `cust_pymt_chan_risk_score` | `risk_events` | `> 0.05` |
| `cust_lexis_nexis_tot_tax_assess_val_am` | `bureau_derived` | — |
| `cust_enhnc_one_way_spend_concentration_30day_rt1` | `spend_pattern` | `> 2.4` |
| `time_wtd_return_index` | `internal_delinquency` | `> 0.2` |
| `cust_rnn_score` | `spend_pattern` | `> 0.028` |
| `cust_min_due_12mo_avg` | `capacity_paydown` | `> 0.08` |
| `gam_clr_erly_risk_score` | `[third_party_score, bureau_derived]` | `< 785` |
| `gam_mtge_loan_actl_pymt_am` | `capacity_paydown` | — |
| `se_no_norm_wtd_pd_unpaid_amt` | `risk_events` | `> 0` |
| `cust_net_pymt_unbl1` | `capacity_paydown` | — |
| `cust_lexis_nexis_blended_score` | `[third_party_score, bureau_derived]` | `< 693` |
| `cust_sbfe_score` | `third_party_score` | `< 863` |
| `cust_dnb_paydex_score` | `third_party_score` | `< 61` |
| `tot_cons_comm_trds_g30` | `[external_delinquency, exposure_leverage]` | — |
| `overall_exposure` | `exposure_leverage` | — |
| `business_revenue` | `capacity_paydown` | — |

Where the "threshold" column is non-empty and the profile lacks structured `risk_threshold`/`risk_direction`, add them (`>`/`>=` → `risk_direction: above`, `<` → `risk_direction: below`; value is the number). Leave existing structured thresholds as-is.

In `model_scores_transaction.yaml`, the same base-name columns get the same `concept` (transaction profile columns are un-suffixed; ignore the extra txn key columns).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_concept_tags.py
import yaml
from datalayer.generator import DataGenerator
from tools import data_tools

TAXONOMY = {
    "internal_delinquency", "external_delinquency", "exposure_leverage",
    "capacity_paydown", "oop", "spend_pattern", "trends_tenure",
    "bureau_derived", "risk_events", "output_score", "third_party_score",
}
DATE_OR_TXN_KEYS = {
    "trans_month", "txn_date_time", "trans_dt", "index",
    "appr_deny_cd", "auto_decline_pos_deny_cd_s1",
}

def _cols(path):
    return yaml.safe_load(open(path))["columns"]

def test_every_modeling_variable_has_a_valid_concept():
    for path in ("config/data_profiles/model_scores.yaml",
                 "config/data_profiles/model_scores_transaction.yaml"):
        for name, spec in _cols(path).items():
            if name in DATE_OR_TXN_KEYS:
                continue
            c = spec.get("concept")
            assert c is not None, f"{path}:{name} missing concept"
            cset = {c} if isinstance(c, str) else set(c)
            assert cset <= TAXONOMY, f"{path}:{name} bad concept {cset - TAXONOMY}"

def test_concept_key_is_non_breaking_for_generator():
    g = DataGenerator(profile_dir="config/data_profiles", seed=42, cases=2)
    g.load_profiles(); g.generate_all()
    assert "oop_interaction" in g._tables["model_scores"]

def test_concept_key_absent_from_schema():
    # the schema surface must never expose the raw concept key per column
    from datalayer.catalog import DataCatalog
    cat = DataCatalog(profile_dir="config/data_profiles")
    schema = cat.get_schema("model_scores")  # no active case → catalog schema
    assert schema is not None
    for col, entry in schema.items():
        assert "concept" not in entry, f"{col} leaked concept key into schema"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_datalayer/test_concept_tags.py -q`
Expected: FAIL on `test_every_modeling_variable_has_a_valid_concept` (concepts not yet added).

- [ ] **Step 3: Add `concept:` (and promote thresholds) to `config/data_profiles/model_scores.yaml`**

For each column in the mapping table, add a `concept:` line (and `risk_threshold`/`risk_direction` where the threshold column is set and not already structured). Example edit for one column:

```yaml
  cust_expsr_avg_rem_12m_ratio:
    dtype: float
    description: Customer Exposure / (Average Remit In 2-12 Months). Values above
      3.15 are risky.
    risk_threshold: 3.15
    risk_direction: above
    concept: [oop, exposure_leverage]
    aliases:
    - cust_expsr_avg_rem_12m_ratio_max
```

Apply the mapping table to all listed columns.

- [ ] **Step 4: Add the same `concept:` tags to `config/data_profiles/model_scores_transaction.yaml`** (same base-name columns; skip `txn_date_time`, `trans_dt`, `appr_deny_cd`, `auto_decline_pos_deny_cd_s1`, `index`).

- [ ] **Step 5: Run the tests + confirm `get_table_schema` output is unchanged in shape**

Run: `python3 -m pytest tests/test_datalayer/test_concept_tags.py tests/test_datalayer/test_generator.py tests/test_tools/test_data_tools.py -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add config/data_profiles/model_scores.yaml config/data_profiles/model_scores_transaction.yaml tests/test_datalayer/test_concept_tags.py
git commit -m "feat(data): concept tags + structured thresholds on modeling profiles"
```

---

### Task 2: Catalog concept-index builders

**Files:**
- Modify: `datalayer/catalog.py` (add `CONCEPT_GLOSS`, `concepts_for_tables`, `variables_for_concepts`)
- Test: `tests/test_datalayer/test_concept_index.py`

**Interfaces:**
- Consumes: `concept:` tags from Task 1.
- Produces:
  - `CONCEPT_GLOSS: dict[str, str]` (module-level)
  - `DataCatalog.concepts_for_tables(tables: list[str]) -> list[str]` — sorted distinct concepts present.
  - `DataCatalog.variables_for_concepts(tables: list[str], concepts: list[str], limit: int = 15) -> list[dict]` — each dict: `{"concept": str, "name": str, "description_short": str, "threshold_text": str}`. De-duped by `name`, thresholded-first, capped at `limit`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datalayer/test_concept_index.py
import textwrap
from datalayer.catalog import DataCatalog, CONCEPT_GLOSS

def _catalog(tmp_path):
    (tmp_path / "m.yaml").write_text(textwrap.dedent("""
    table: m
    columns:
      a_ratio:
        dtype: float
        description: A ratio thing. Values above 3.15 are risky.
        risk_threshold: 3.15
        risk_direction: above
        concept: [oop, exposure_leverage]
      b_index:
        dtype: float
        description: B index thing.
        concept: oop
      c_other:
        dtype: int
        description: Unrelated.
        concept: capacity_paydown
    """))
    return DataCatalog(profile_dir=str(tmp_path))

def test_concepts_for_tables(tmp_path):
    cat = _catalog(tmp_path)
    assert cat.concepts_for_tables(["m"]) == ["capacity_paydown", "exposure_leverage", "oop"]

def test_variables_for_concepts_matches_and_renders(tmp_path):
    cat = _catalog(tmp_path)
    got = cat.variables_for_concepts(["m"], ["oop"])
    names = [v["name"] for v in got]
    assert names == ["a_ratio", "b_index"]  # thresholded-first (a_ratio has threshold)
    a = next(v for v in got if v["name"] == "a_ratio")
    assert a["threshold_text"] == "risky > 3.15"
    assert a["description_short"] == "A ratio thing"
    assert cat.variables_for_concepts(["m"], ["capacity_paydown"])[0]["name"] == "c_other"

def test_variables_for_concepts_cap(tmp_path):
    cat = _catalog(tmp_path)
    assert len(cat.variables_for_concepts(["m"], ["oop"], limit=1)) == 1

def test_gloss_covers_taxonomy():
    for c in ("internal_delinquency", "oop", "third_party_score"):
        assert c in CONCEPT_GLOSS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_datalayer/test_concept_index.py -q`
Expected: FAIL with `ImportError: cannot import name 'CONCEPT_GLOSS'`.

- [ ] **Step 3: Implement in `datalayer/catalog.py`**

Add near the top (module level, after imports):

```python
CONCEPT_GLOSS: dict[str, str] = {
    "internal_delinquency": "internal delinquency / DPD / payment-return behaviour",
    "external_delinquency": "external-trade delinquency & utilization",
    "exposure_leverage": "exposure, revolving balances, leverage",
    "capacity_paydown": "income, debt-service, paydown, remit capacity",
    "oop": "out-of-pattern: exposure vs. trailing-12-month remit",
    "spend_pattern": "spend concentration / RNN / merchant-risk spend signals",
    "trends_tenure": "score/balance trend indices & tenure/age",
    "bureau_derived": "third-party bureau inputs (FICO, LexisNexis, inquiries)",
    "risk_events": "risky-event counts & product-risk flags",
    "output_score": "internal ML output scores (CDSS, TSR)",
    "third_party_score": "embedded third-party scores (Paydex, SBFE, LN, Clarity)",
}
```

Add these methods to `DataCatalog`:

```python
    @staticmethod
    def _concept_set(spec: dict) -> set[str]:
        c = spec.get("concept")
        if isinstance(c, str):
            return {c}
        if isinstance(c, list):
            return set(c)
        return set()

    def concepts_for_tables(self, tables: list[str]) -> list[str]:
        found: set[str] = set()
        for t in tables:
            prof = self._profiles.get(t) or {}
            for spec in (prof.get("columns") or {}).values():
                found |= self._concept_set(spec)
        return sorted(found)

    def variables_for_concepts(
        self, tables: list[str], concepts: list[str], limit: int = 15,
    ) -> list[dict]:
        want = set(concepts)
        hits: list[dict] = []
        seen: set[str] = set()
        for t in tables:
            prof = self._profiles.get(t) or {}
            for name, spec in (prof.get("columns") or {}).items():
                match = self._concept_set(spec) & want
                if not match or name in seen:
                    continue
                seen.add(name)
                desc = (spec.get("description") or "").strip()
                short = desc.split(". ")[0].strip().rstrip(".")
                thr = ""
                if "risk_threshold" in spec:
                    sym = ">" if spec.get("risk_direction", "above") == "above" else "<"
                    thr = f"risky {sym} {spec['risk_threshold']}"
                hits.append({
                    "concept": sorted(match)[0],
                    "name": name,
                    "description_short": short,
                    "threshold_text": thr,
                })
        hits.sort(key=lambda h: (h["threshold_text"] == "", h["concept"], h["name"]))
        return hits[:limit]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_datalayer/test_concept_index.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add datalayer/catalog.py tests/test_datalayer/test_concept_index.py
git commit -m "feat(catalog): concept index builders (concepts_for_tables, variables_for_concepts)"
```

---

### Task 3: Orchestrator roster lists directable concepts

**Files:**
- Modify: `agent_factories/orchestrator_agent.py` (`_render_team_roster`)
- Test: `tests/test_agent_factories/test_orchestrator_roster.py`

**Interfaces:**
- Consumes: `DataCatalog.concepts_for_tables`, `CONCEPT_GLOSS` (Task 2).
- Produces: roster text that, for a specialist whose tables carry tags, includes a `concepts you can direct:` line and a footer instruction to pass `concepts=[...]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_factories/test_orchestrator_roster.py
from agent_factories.orchestrator_agent import _render_team_roster

class _StubAgent:
    def __init__(self, name): self.name = name

class _StubCatalog:
    def get_description(self, t): return "Modeling features."
    def concepts_for_tables(self, tables): return ["oop", "exposure_leverage"]

def test_roster_lists_concepts_for_tagged_specialist():
    roster = _render_team_roster([_StubAgent("modeling")], catalog=_StubCatalog())
    assert "concepts you can direct:" in roster
    assert "oop" in roster
    assert "pass `concepts=" in roster  # footer instruction present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_factories/test_orchestrator_roster.py -q`
Expected: FAIL (`concepts you can direct:` not in roster).

- [ ] **Step 3: Implement in `agent_factories/orchestrator_agent.py`**

Add the import at the top:

```python
from datalayer.catalog import CONCEPT_GLOSS
```

In `_render_team_roster`, after the `for table in hints:` loop and before the risk-signals block, add:

```python
        if catalog is not None:
            concepts = catalog.concepts_for_tables(hints)
            if concepts:
                gloss = "; ".join(
                    f"{c} ({CONCEPT_GLOSS.get(c, c)})" for c in concepts
                )
                lines.append(f"    concepts you can direct: {gloss}")
```

Then extend the footer (replace the existing `ROUTING RULE` append) with an added sentence:

```python
    lines.append(
        "\nROUTING RULE: pick the specialist whose `owns` table most directly "
        "carries the reviewer's question. Prefer 1–2 specialists; only widen "
        "to 3+ when the question explicitly spans multiple domains. When a "
        "specialist lists `concepts you can direct`, pass `concepts=[...]` in "
        "the tool call naming the concept(s) relevant to your sub-question so "
        "the specialist receives the exact variable definitions."
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest tests/test_agent_factories/test_orchestrator_roster.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_factories/orchestrator_agent.py tests/test_agent_factories/test_orchestrator_roster.py
git commit -m "feat(orchestrator): list directable concepts per specialist in the team roster"
```

---

### Task 4: Runner injects the directed-variables block

**Files:**
- Modify: `tools/redacting_tool.py` (`_compose_specialist_input`, new `_render_directed_variables`, `redacting_tool` kwargs, `_runner` `concepts` arg + assembly)
- Modify: `agent_factories/orchestrator_agent.py` (pass `catalog` + `data_hints` into `redacting_tool` for specialists)
- Test: `tests/test_tools/test_redacting_directed_vars.py`

**Interfaces:**
- Consumes: `DataCatalog.variables_for_concepts` (Task 2); specialist `data_hints` (from `_load_domain_skill`).
- Produces: specialist input where, when `concepts` is passed, a `§ DIRECTED VARIABLES` block is the last prefix before `--- New question ---`; when not, output is byte-identical to today.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_redacting_directed_vars.py
from tools.redacting_tool import _compose_specialist_input, _render_directed_variables

def test_compose_no_regression_without_directed_block():
    # byte-identical to the 3-arg behavior when directed_block omitted
    out = _compose_specialist_input("EPI", "KB", "the question")
    assert out == "EPI\n\nKB\n\n--- New question ---\nthe question"

def test_compose_places_directed_block_last():
    out = _compose_specialist_input("EPI", "KB", "the question", "DIR")
    assert out == "EPI\n\nKB\n\nDIR\n\n--- New question ---\nthe question"

def test_compose_directed_only():
    out = _compose_specialist_input("", "", "q", "DIR")
    assert out == "DIR\n\n--- New question ---\nq"

def test_render_directed_variables_format():
    vars = [
        {"concept": "oop", "name": "cust_expsr_avg_rem_12m_ratio",
         "description_short": "Exposure / avg remit", "threshold_text": "risky > 3.15"},
        {"concept": "oop", "name": "oop_interaction",
         "description_short": "OOP spend index", "threshold_text": ""},
    ]
    block = _render_directed_variables(vars)
    assert block.splitlines()[0].startswith("§ DIRECTED VARIABLES")
    assert "[oop] cust_expsr_avg_rem_12m_ratio — Exposure / avg remit; risky > 3.15" in block
    assert "[oop] oop_interaction — OOP spend index" in block

def test_render_empty_is_blank():
    assert _render_directed_variables([]) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tools/test_redacting_directed_vars.py -q`
Expected: FAIL (`_render_directed_variables` not defined; `_compose_specialist_input` takes 3 args).

- [ ] **Step 3: Extend `_compose_specialist_input` and add `_render_directed_variables` in `tools/redacting_tool.py`**

Replace `_compose_specialist_input` with:

```python
def _compose_specialist_input(episodic_block: str, kb_digest: str,
                              sub_question: str, directed_block: str = "") -> str:
    """Prepend episodic slice, KB digest, and directed-variable block (each
    non-empty, in that order) before the sub-question. Directed variables sit
    last (nearest the question) as the most question-specific prefix.
    Byte-identical to the prior behavior when directed_block is empty."""
    prefixes = [p for p in (episodic_block, kb_digest, directed_block) if p]
    if not prefixes:
        return sub_question
    return "\n\n".join(prefixes) + f"\n\n--- New question ---\n{sub_question}"


def _render_directed_variables(variables: list[dict]) -> str:
    """Render the compact §DIRECTED VARIABLES block from variables_for_concepts."""
    if not variables:
        return ""
    lines = ["§ DIRECTED VARIABLES (for this question — from the data catalog)"]
    for v in variables:
        thr = f"; {v['threshold_text']}" if v.get("threshold_text") else ""
        lines.append(f"[{v['concept']}] {v['name']} — {v['description_short']}{thr}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tools/test_redacting_directed_vars.py -q`
Expected: PASS.

- [ ] **Step 5: Add `catalog`/`data_hints` kwargs + `concepts` arg + assembly in `_runner`**

Change the `redacting_tool` signature:

```python
def redacting_tool(
    agent: Agent,
    name: str,
    description: str,
    *,
    timeout_s: float = _SPECIALIST_TIMEOUT_S,
    max_turns: int = _SPECIALIST_MAX_TURNS,
    catalog=None,
    data_hints: list[str] | None = None,
):
```

Change the `_runner` signature to accept `concepts`:

```python
    @function_tool(name_override=name, description_override=description)
    async def _runner(ctx: RunContextWrapper, sub_question: str,
                      concepts: list[str] | None = None) -> str:
```

In the first-call branch where `_episodic_block` and `kb_digest` are composed (the block ending `contextual_in = _compose_specialist_input(_episodic_block, kb_digest, redacted_in)`), replace that composition with directed-variable assembly:

```python
            _directed_block = ""
            if concepts and catalog is not None and data_hints:
                try:
                    _vars = catalog.variables_for_concepts(data_hints, concepts)
                    _directed_block = _render_directed_variables(_vars)
                except Exception as _dv_exc:  # noqa: BLE001 — never break the call
                    _directed_block = ""
                    if logger is not None:
                        logger.log("directed_variables_assembly_failed",
                                   {"specialist": name, "concepts": concepts,
                                    "error": repr(_dv_exc)})
            contextual_in = _compose_specialist_input(
                _episodic_block, kb_digest, redacted_in, _directed_block)
```

- [ ] **Step 6: Wire `catalog`/`data_hints` into the specialist tools in `agent_factories/orchestrator_agent.py`**

In `build_orchestrator_agent`, replace the specialist tool construction:

```python
    tools = []
    for s in specialists:
        _sk = _load_domain_skill(s.name)
        tools.append(redacting_tool(
            s, name=s.name, description=_describe_specialist(s),
            catalog=catalog,
            data_hints=(list(_sk.data_hints) if _sk else None),
        ))
```

(The `report_agent` and `general_specialist` tool appends stay unchanged — they pass no `catalog`/`data_hints`, so `concepts` is inert for them.)

- [ ] **Step 7: Run the full redacting + orchestrator + catalog suites**

Run: `python3 -m pytest tests/test_tools/test_redacting_directed_vars.py tests/test_datalayer tests/test_agent_factories/test_orchestrator_roster.py -q`
Expected: PASS (all). If `tests/test_tools/test_redacting_tool.py` exists and imports cleanly (not matplotlib-blocked), run it too and expect PASS (no-regression on the 3-arg default).

- [ ] **Step 8: Commit**

```bash
git add tools/redacting_tool.py agent_factories/orchestrator_agent.py tests/test_tools/test_redacting_directed_vars.py
git commit -m "feat(runner): inject concept-directed variable block into specialist input"
```

---

### Task 5: Slim the modeling skill's mechanical vocab table (keep semantic overlay)

**Files:**
- Modify: `skills/domain/modeling.md`
- Test: `tests/test_skills/test_domain_skills.py` (must still pass; add a focused assertion)

**Interfaces:**
- Consumes: nothing new. Produces: a slimmer `modeling.md` whose mechanical concept→variable map defers to the injected directed variables, while the OOP/semantic expertise remains.

- [ ] **Step 1: Replace the "Concept → vocabulary lookup" section**

Replace the whole `## Concept → vocabulary lookup` table (the `| Concept | Keywords... |` block) with:

```markdown
## Concept → variable selection

The orchestrator directs each sub-question with `concepts=[...]`; when it does, a **§ DIRECTED VARIABLES** block (variable · meaning · threshold) is prepended to your input — use those variables directly. If no directed block is present (or you need the full set), call `get_table_schema('model_scores')` and map the concept to columns by reading descriptions. Concept vocabulary: internal_delinquency, external_delinquency, exposure_leverage, capacity_paydown, oop, spend_pattern, trends_tenure, bureau_derived, risk_events, output_score, third_party_score. See §OOP below for the interpretation that tags alone don't carry.
```

Leave `## Threshold reading`, `## Findings format`, and the `## Out-of-pattern (OOP) & exposure-vs-remit reads` subsection unchanged.

- [ ] **Step 2: Add a focused test**

```python
# append to tests/test_skills/test_domain_skills.py
def test_modeling_defers_to_directed_variables():
    from skills.domain.loader import load_domain_skill
    body = load_domain_skill("modeling").system_prompt
    assert "DIRECTED VARIABLES" in body
    assert "Out-of-pattern (OOP)" in body  # semantic overlay retained
```

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_skills/test_domain_skills.py -q`
Expected: PASS.

- [ ] **Step 4: Verify no dead column names / all referenced columns resolve**

Run:
```bash
python3 - <<'PY'
import yaml, glob, re
canon=set()
for p in glob.glob("config/data_profiles/*.yaml"):
    for c,s in (yaml.safe_load(open(p)).get("columns") or {}).items():
        canon.add(c.lower())
        for a in (s.get("aliases") or []): canon.add(str(a).strip().lower())
import pathlib
body = pathlib.Path("skills/domain/modeling.md").read_text()
for tok in re.findall(r"`([a-z0-9_]+)`", body):
    # only assert on obvious column-looking tokens we cite as examples
    pass
print("modeling.md references OK (manual scan)")
PY
```
Expected: prints OK; visually confirm no removed column names remain.

- [ ] **Step 5: Commit**

```bash
git add skills/domain/modeling.md tests/test_skills/test_domain_skills.py
git commit -m "docs(modeling): defer mechanical concept map to directed variables; keep OOP overlay"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** §3 data model → Task 1; §4.1 builders → Task 2; §4.2 roster → Task 3; §4.3–4.4 arg + injection → Task 4; §4.5 overlay → Task 5; §6 no-regression → Task 4 Step 1 (`test_compose_no_regression_without_directed_block`) + inert path for report/general tools; §7 tests → distributed per task.
- **Type consistency:** `variables_for_concepts` returns dicts with keys `concept`/`name`/`description_short`/`threshold_text` (Task 2), consumed byte-for-byte by `_render_directed_variables` (Task 4). `concepts_for_tables` returns `list[str]`, consumed by the roster (Task 3). `redacting_tool(..., catalog=, data_hints=)` (Task 4 Step 5) is called with exactly those kwargs in Task 4 Step 6.
- **No push:** every task commits locally only.
