"""Pure view-model builder for the /catalog page.

Reads DataCatalog, context_dict, and Provenance — no pandas, no LLM calls.
Output shape is consumed by Task V3 (the Flask /catalog route).
"""
from __future__ import annotations

import os
import re as _re

from datalayer.catalog import DataCatalog
from datalayer.gateway import LocalDataGateway
from datalayer.provenance import Provenance
import datalayer.context_dict as cd


# Fields checked when computing per-column provenance aggregate.
_PROVENANCE_FIELDS = ("description", "risk_threshold")

_DESC_MAX_CHARS = 140


def _description_short(text: str) -> str:
    """Return a truncated version of *text* for display.

    Strategy: use the first sentence (up to the first '.', '!', or '?')
    when that is <= _DESC_MAX_CHARS, otherwise truncate at _DESC_MAX_CHARS.
    Appends a unicode ellipsis when truncation occurs.
    Returns *text* unchanged when it is already short enough.
    """
    if len(text) <= _DESC_MAX_CHARS:
        return text
    # Look for the first sentence boundary within the first _DESC_MAX_CHARS chars.
    m = _re.search(r'[.!?]', text[:_DESC_MAX_CHARS])
    if m:
        truncated = text[:m.end()].rstrip()
        # Only append ellipsis when the truncated form differs from the full text.
        return truncated + ("…" if truncated != text else "")
    # No sentence boundary — hard-truncate.
    return text[:_DESC_MAX_CHARS].rstrip() + "…"


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


def _load_present_tables(data_dir: str | None) -> set[str] | None:
    """Return the set of table names present across all cases in data_dir.

    Returns None (not an empty set) when the gateway is unusable, so callers
    can distinguish "no data_dir" from "data_dir exists but has no tables".
    """
    if not data_dir or not os.path.isdir(data_dir):
        return None
    try:
        gw = LocalDataGateway.from_case_folders(data_dir)
        case_ids = gw.list_case_ids()
        if not case_ids:
            return None
        present: set[str] = set()
        for cid in case_ids:
            gw.set_case(cid)
            present.update(gw.list_tables())
        return present if present else None
    except Exception:
        return None


def build_catalog_view(
    profile_dir: str,
    context_dir: str,
    provenance_path: str,
    data_dir: str | None = None,
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
    data_dir:
        Optional path to the case-folders root (e.g. ``data_tables/real``).
        When provided and non-empty, used to mark profiles as ``stale=True``
        when no backing CSV table exists.  If None, missing, or empty,
        all profiles are marked ``stale=False`` (graceful fallback).

    Returns
    -------
    dict with shape::

        {
            "tables": [
                {
                    "table": str,
                    "description": str,
                    "aliases": list[str],
                    "stale": bool,
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

    present_tables = _load_present_tables(data_dir)

    tables_out: list[dict] = []

    for table_name in catalog.list_tables():
        profile = catalog._profiles[table_name]
        description = profile.get("description", "")
        aliases = list(profile.get("aliases") or [])
        columns_spec: dict = profile.get("columns") or {}

        # Staleness: live when table name OR any alias matches a present table.
        if present_tables is None:
            stale = False
        else:
            candidate_names = {table_name} | set(aliases)
            stale = candidate_names.isdisjoint(present_tables)

        # Context entries for this table (var_name -> ContextEntry)
        table_ctx: dict = context_by_table.get(table_name, {})
        profile_col_names: set[str] = set(columns_spec.keys())

        # Build a normalized-key index for the context vars to support
        # Title-Case context names (e.g. "FICO Score") matching snake_case
        # profile columns (e.g. "fico_score").
        ctx_norm: dict[str, str] = {
            cd.normalize_key(var): var for var in table_ctx
        }
        # Build a normalized-key set for profile columns for context_only logic.
        col_norm: set[str] = {cd.normalize_key(c) for c in profile_col_names}

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

            # in_context: exact match first, then normalized match.
            in_context = (col_name in table_ctx) or (cd.normalize_key(col_name) in ctx_norm)

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

        # context_only: context vars whose normalized key matches NO profile column.
        context_only: list[str] = [
            var for var in table_ctx
            if cd.normalize_key(var) not in col_norm
        ]

        tables_out.append({
            "table": table_name,
            "description": description,
            "description_short": _description_short(description),
            "aliases": aliases,
            "stale": stale,
            "columns": columns_out,
            "context_only": context_only,
        })

    # ── Exclude stale tables entirely ─────────────────────────────────────────
    # Tables with no backing data file are dropped before grouping.
    # When data_dir is None/missing, present_tables is None and stale is always
    # False, so nothing is dropped (graceful fallback preserved).
    tables_out = [t for t in tables_out if not t["stale"]]

    # ── Grouping ──────────────────────────────────────────────────────────────
    # Derive a group key by stripping a trailing "_transaction" suffix.
    # Tables sharing a group key are rendered together; within a group the
    # base (monthly) table is listed first, then the _transaction variant.

    def _group_key(table_name: str) -> str:
        if table_name.endswith("_transaction"):
            return table_name[: -len("_transaction")]
        return table_name

    for t in tables_out:
        t["group_key"] = _group_key(t["table"])

    # Collect groups: maps group_key → list of table dicts, base before txn.
    from collections import OrderedDict
    groups_map: dict[str, list[dict]] = OrderedDict()
    for t in tables_out:
        groups_map.setdefault(t["group_key"], []).append(t)

    # Within each group: base table (no suffix) first, then _transaction.
    def _group_sort_key(t: dict) -> int:
        return 1 if t["table"].endswith("_transaction") else 0

    for members in groups_map.values():
        members.sort(key=_group_sort_key)

    # Build the groups list.  Since stale entries are excluded, all_stale is
    # always False; keep the key for interface compatibility.
    groups_list: list[dict] = [
        {
            "key": key,
            "tables": members,
            "all_stale": False,
        }
        for key, members in groups_map.items()
    ]

    # Sort groups alphabetically by key (all are live after exclusion).
    groups_list.sort(key=lambda g: g["key"])

    # Rebuild the flat tables list so group members are adjacent and groups are
    # in the sorted order.
    tables_out = [t for g in groups_list for t in g["tables"]]

    return {"tables": tables_out, "groups": groups_list}
