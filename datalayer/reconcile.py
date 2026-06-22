"""Manager-triggered reconciliation: tables ↔ profiles ↔ context dictionaries.

Pure-Python orchestration. Dtype/sample validation is delegated to adapter
helpers (the only pandas-importing module). LLM judgment is delegated to the
DataManagerAgent passed in by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConsistencyResult:
    uniform_schema: dict[str, set]
    flags: list[str] = field(default_factory=list)


def check_consistency(gateway) -> ConsistencyResult:
    case_ids = gateway.list_case_ids()
    # {table: {case_id: column_set}}
    per_case: dict[str, dict[str, set]] = {}
    for cid in case_ids:
        gateway.set_case(cid)
        for table in gateway.list_tables():
            rows = gateway.query(table) or []
            cols = set(rows[0].keys()) if rows else set()
            per_case.setdefault(table, {})[cid] = cols

    uniform: dict[str, set] = {}
    flags: list[str] = []
    for table, by_case in per_case.items():
        present_cases = set(by_case)
        column_sets = list(by_case.values())
        all_present = present_cases == set(case_ids)
        all_equal = all(c == column_sets[0] for c in column_sets)
        if all_present and all_equal:
            uniform[table] = column_sets[0]
        else:
            ref = max(column_sets, key=len) if column_sets else set()
            divergent = [
                cid for cid, cols in by_case.items() if cols != ref
            ] + [cid for cid in case_ids if cid not in present_cases]
            flags.append(
                f"[schema-divergence] table '{table}' differs across cases: {sorted(divergent)}"
            )
    return ConsistencyResult(uniform_schema=uniform, flags=flags)


# ---------------------------------------------------------------------------
# Task 7: Reconciliation orchestrator
# ---------------------------------------------------------------------------

KNOWLEDGE_BRIEF = (
    "CDSS = credit_loss_prob (default likelihood, next 18m). "
    "TSR = tot_struct_risk_score (overall structural risk). "
    "Use the pillar vocabulary for credit-risk terms."
)


@dataclass
class ReconcileResult:
    writes: list = field(default_factory=list)
    flags: list = field(default_factory=list)


async def reconcile(
    gateway,
    catalog,
    agent,
    context_by_table,
    provenance,
    *,
    confidence_min: float = 0.75,
) -> ReconcileResult:
    """Run the full reconciliation pipeline.

    1. Consistency check → surface schema-divergence flags.
    2. For each uniform table, match real columns to canonical schema (exact
       first, agent fallback gated by confidence_min).
    3. For each matched column with a context entry: apply threshold (regex
       pre-parsed value first, agent normalize fallback) and polish the
       description.
    4. Provenance gate: only write fields the agent owns (never clobber human
       edits).  Emit [human-owned] flags for skipped fields.
    5. Write accepted patches via catalog.write_profile_patch and record
       provenance baselines.

    Flag taxonomy: [schema-divergence], [context-only], [table-only],
    [unresolved], [human-owned].
    """
    cons = check_consistency(gateway)
    flags: list[str] = list(cons.flags)
    writes: list[tuple[str, str, str]] = []

    for table, columns in cons.uniform_schema.items():
        schema = catalog.get_schema(table) or {}
        ctx = context_by_table.get(table, {})
        canonical_cols = list(schema.keys())

        # Flag context entries that reference a column absent from the data.
        for var in ctx:
            if var not in columns and var not in canonical_cols:
                flags.append(
                    f"[context-only] '{table}.{var}' in dictionary but not in data"
                )

        for real_col in sorted(columns):
            # Resolve canonical column — exact match first, then agent.
            if real_col in schema:
                canonical = real_col
            else:
                match = await agent.match_column(
                    real_col, _samples(gateway, table, real_col), canonical_cols
                )
                if match["canonical_col"] and match["confidence"] >= confidence_min:
                    canonical = match["canonical_col"]
                else:
                    flags.append(
                        f"[unresolved] '{table}.{real_col}' "
                        f"(conf {match['confidence']:.2f})"
                    )
                    continue

            entry = ctx.get(canonical) or ctx.get(real_col)
            if entry is None:
                flags.append(
                    f"[table-only] '{table}.{canonical}' has no dictionary entry"
                )
                continue

            # Threshold: regex-parsed value on the entry takes precedence;
            # fall back to agent normalization of raw text.
            thr = entry.threshold or await agent.normalize_threshold_text(
                entry.threshold_text
            )
            # Description polished via agent (grounded with domain brief).
            desc = await agent.polish_description(
                canonical, entry.raw_description, KNOWLEDGE_BRIEF
            )

            # Build candidate patch fields.
            candidates: list[tuple[str, object]] = [("description", desc)]
            if thr:
                rt = thr.get("risk_threshold")
                rd = thr.get("risk_direction")
                if rt is not None:
                    candidates.append(("risk_threshold", rt))
                if rd is not None:
                    candidates.append(("risk_direction", rd))

            col_patch: dict[str, object] = {}
            for fieldname, value in candidates:
                if value is None:
                    continue
                # Provenance gate: compare against the LIVE profile value.
                live = (
                    catalog._profiles.get(table, {})
                    .get("columns", {})
                    .get(canonical, {})
                    .get(fieldname)
                )
                if not provenance.is_agent_owned(table, canonical, fieldname, live):
                    flags.append(
                        f"[human-owned] '{table}.{canonical}.{fieldname}' "
                        f"left as human value"
                    )
                    continue
                col_patch[fieldname] = value

            if col_patch:
                catalog.write_profile_patch(table, {"columns": {canonical: col_patch}})
                for fieldname, value in col_patch.items():
                    provenance.record(table, canonical, fieldname, value)
                    writes.append((table, canonical, fieldname))

    provenance.save()
    return ReconcileResult(writes=writes, flags=flags)


def _samples(gateway, table: str, col: str, limit: int = 10) -> list:
    """Collect up to *limit* non-null sample values for *col* from *table*."""
    out: list = []
    for cid in gateway.list_case_ids():
        gateway.set_case(cid)
        for row in (gateway.query(table) or []):
            v = row.get(col)
            if v not in (None, ""):
                out.append(v)
                if len(out) >= limit:
                    return out
    return out
