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
