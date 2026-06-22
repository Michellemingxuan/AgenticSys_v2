"""Pure view-model builder for the /catalog page.

Reads DataCatalog, context_dict, and Provenance — no pandas, no LLM calls.
Output shape is consumed by Task V3 (the Flask /catalog route).
"""
from __future__ import annotations

from datalayer.catalog import DataCatalog
from datalayer.provenance import Provenance
import datalayer.context_dict as cd


# Fields checked when computing per-column provenance aggregate.
_PROVENANCE_FIELDS = ("description", "risk_threshold")


def _aggregate_provenance(
    pv: Provenance,
    table: str,
    col: str,
    spec: dict,
) -> str:
    """Return 'human' | 'agent' | 'unmanaged' for a column.

    Priority: human > agent > unmanaged.
    Checks the 'description' and 'risk_threshold' fields only
    (the two fields the reconcile agent manages).
    """
    statuses: list[str] = []
    for field in _PROVENANCE_FIELDS:
        if field not in spec:
            continue
        current_value = spec[field]
        statuses.append(pv.ownership(table, col, field, current_value))

    if not statuses:
        # Column has none of the managed fields — use description if present.
        desc = spec.get("description", "")
        statuses = [pv.ownership(table, col, "description", desc)]

    if "human" in statuses:
        return "human"
    if "agent" in statuses:
        return "agent"
    return "unmanaged"


def build_catalog_view(
    profile_dir: str,
    context_dir: str,
    provenance_path: str,
) -> dict:
    """Build the catalog view-model dict.

    Parameters
    ----------
    profile_dir:
        Path to the directory containing ``*.yaml`` data-profile files.
    context_dir:
        Path to the directory containing ``*_context_description.txt`` files.
    provenance_path:
        Path to the ``.provenance.json`` sidecar file.

    Returns
    -------
    dict with shape::

        {
            "tables": [
                {
                    "table": str,
                    "description": str,
                    "aliases": list[str],
                    "columns": [
                        {
                            "name": str,
                            "dtype": str,
                            "parse_hint": str | None,
                            "description": str,
                            "threshold": {"value": ..., "direction": str} | None,
                            "in_context": bool,
                            "provenance": "human" | "agent" | "unmanaged",
                        },
                        ...
                    ],
                    "context_only": [var_name, ...],
                },
                ...
            ]
        }
    """
    catalog = DataCatalog(profile_dir)
    context_by_table = cd.load_context_by_table(context_dir)
    pv = Provenance(provenance_path)

    tables_out: list[dict] = []

    for table_name in catalog.list_tables():
        profile = catalog._profiles[table_name]
        description = profile.get("description", "")
        aliases = list(profile.get("aliases") or [])
        columns_spec: dict = profile.get("columns") or {}

        # Context entries for this table (var_name -> ContextEntry)
        table_ctx: dict = context_by_table.get(table_name, {})
        profile_col_names: set[str] = set(columns_spec.keys())

        columns_out: list[dict] = []
        for col_name, spec in columns_spec.items():
            dtype = spec.get("dtype", "unknown")
            parse_hint = spec.get("parse_hint")
            col_description = spec.get("description", "")

            # Threshold
            threshold: dict | None = None
            if "risk_threshold" in spec:
                threshold = {
                    "value": spec["risk_threshold"],
                    "direction": spec.get("risk_direction", "above"),
                }

            in_context = col_name in table_ctx

            provenance = _aggregate_provenance(pv, table_name, col_name, spec)

            columns_out.append({
                "name": col_name,
                "dtype": dtype,
                "parse_hint": parse_hint,
                "description": col_description,
                "threshold": threshold,
                "in_context": in_context,
                "provenance": provenance,
            })

        # context_only: context vars not present as profile columns
        context_only: list[str] = [
            var for var in table_ctx if var not in profile_col_names
        ]

        tables_out.append({
            "table": table_name,
            "description": description,
            "aliases": aliases,
            "columns": columns_out,
            "context_only": context_only,
        })

    return {"tables": tables_out}
