"""Data catalog backed by YAML profile definitions.

The catalog is the single source of truth for what data is available.
Specialists use it to understand what tables and columns exist.
The Text-to-SQL skill (future) reads it to resolve semantic queries.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class DataCatalog:
    """Loads table metadata from YAML data-profile configs.

    Provides schema information in two formats:
    - Structured (get_schema): for programmatic use
    - Prompt context (to_prompt_context): for injection into LLM prompts
    """

    def __init__(self, profile_dir: str = "config/data_profiles"):
        self._profiles: dict[str, dict] = {}
        self._profile_dir = Path(profile_dir)
        self._load()

    def _load(self) -> None:
        for path in sorted(self._profile_dir.glob("*.yaml")):
            with open(path) as f:
                profile = yaml.safe_load(f)
            self._profiles[profile["table"]] = profile

    def list_tables(self) -> list[str]:
        return sorted(self._profiles.keys())

    def get_schema(self, table_name: str) -> dict | None:
        """Return column schema: {col_name: {type, description}}."""
        profile = self._profiles.get(table_name)
        if profile is None:
            return None
        schema: dict[str, dict] = {}
        for col_name, spec in profile["columns"].items():
            schema[col_name] = {
                "type": spec["dtype"],
                "description": spec.get("description", ""),
            }
        return schema

    def get_description(self, table_name: str) -> str:
        profile = self._profiles.get(table_name)
        if profile is None:
            return ""
        return profile.get("description", "")

    def get_column_details(self, table_name: str) -> dict | None:
        """Return full column details including distribution info.
        Useful for understanding data ranges and typical values."""
        profile = self._profiles.get(table_name)
        if profile is None:
            return None
        details: dict[str, dict] = {}
        for col_name, spec in profile["columns"].items():
            col_info: dict = {
                "type": spec["dtype"],
                "description": spec.get("description", ""),
            }
            # Include range/distribution info for context
            if "min" in spec:
                col_info["min"] = spec["min"]
            if "max" in spec:
                col_info["max"] = spec["max"]
            if "categories" in spec:
                col_info["values"] = list(spec["categories"].keys())
            if "distribution" in spec:
                col_info["distribution"] = spec["distribution"]
            details[col_name] = col_info
        return details

    def write_profile_patch(self, table: str, patch: dict) -> None:
        """Merge a patch dict into the table's YAML profile and persist.

        Creates the profile file if the table doesn't exist yet. Appends
        to list-valued fields (e.g., aliases) instead of overwriting. Dicts
        merge recursively; scalars in the patch overwrite the existing value.

        After writing, the in-memory ``_profiles`` dict is refreshed from
        disk so the catalog stays consistent.

        Note: ``yaml.safe_dump`` does NOT preserve comments — any comments
        in pre-existing profile YAMLs will be dropped on first patch. See
        docs/plans/2026-04-24-data-catalog-sync.md § Known Issues.
        """
        profile_path = self._profile_dir / f"{table}.yaml"

        if profile_path.exists():
            with open(profile_path) as f:
                profile = yaml.safe_load(f) or {}
        else:
            profile = {"table": table, "description": "", "columns": {}}

        self._merge_patch(profile, patch)

        with open(profile_path, "w") as f:
            yaml.safe_dump(profile, f, default_flow_style=False, sort_keys=False)

        self._profiles[table] = profile

    @staticmethod
    def _merge_patch(base: dict, patch: dict) -> None:
        """Recursive merge. Lists are union-appended (dedup preserving order)."""
        for key, value in patch.items():
            if key not in base or base[key] is None:
                base[key] = value
                continue
            existing = base[key]
            if isinstance(existing, dict) and isinstance(value, dict):
                DataCatalog._merge_patch(existing, value)
            elif isinstance(existing, list) and isinstance(value, list):
                for item in value:
                    if item not in existing:
                        existing.append(item)
            else:
                base[key] = value

    def to_prompt_context(
        self,
        case_schema: dict[str, list[str]] | None = None,
    ) -> str:
        """Format the catalog as text for injection into LLM prompts.

        Parameters
        ----------
        case_schema : dict[str, list[str]] | None
            Optional per-case filter: ``{table_name: [real_column_names]}``.
            When provided, output includes only tables that are physically
            present in this case, and renders columns using their real
            names (annotated with ``[canonical: X]`` when they differ).
            When None, renders the full catalog using canonical names
            (backwards-compatible with pre-sync behavior).
        """
        lines: list[str] = ["=== DATA CATALOG ===", ""]

        if case_schema is not None:
            scope_tables = [t for t in self.list_tables() if t in case_schema]
            # Pre-scan for banner — emit if any column in scope is pending.
            scope_has_pending = False
            for table in scope_tables:
                profile = self._profiles[table]
                for real_col in case_schema[table]:
                    spec = self._find_column_spec(profile, real_col)
                    if spec and spec.get("description_pending"):
                        scope_has_pending = True
                        break
                if scope_has_pending:
                    break
            if scope_has_pending:
                lines.append(
                    "⚠ Some columns in this case have unverified descriptions — "
                    "treat them cautiously."
                )
                lines.append("")
        else:
            scope_tables = self.list_tables()

        for table in scope_tables:
            profile = self._profiles[table]
            desc = profile.get("description", "")
            lines.append(f"TABLE: {table}")
            lines.append(f"  {desc}")

            if case_schema is not None:
                for real_col in case_schema[table]:
                    spec = self._find_column_spec(profile, real_col)
                    if spec is None:
                        lines.append(f"  - {real_col} [unknown]: (not in catalog)")
                        continue
                    canonical = self._canonical_of(profile, real_col)
                    lines.append(self._format_column_line(
                        real_col=real_col,
                        canonical_col=canonical,
                        spec=spec,
                    ))
            else:
                for col, spec in profile.get("columns", {}).items():
                    lines.append(self._format_column_line(
                        real_col=col,
                        canonical_col=col,
                        spec=spec,
                    ))
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _find_column_spec(profile: dict, real_col: str) -> dict | None:
        """Find the column spec for a real name, checking canonical and aliases."""
        columns = profile.get("columns", {}) or {}
        if real_col in columns:
            return columns[real_col]
        for spec in columns.values():
            if real_col in (spec.get("aliases") or []):
                return spec
        return None

    @staticmethod
    def _canonical_of(profile: dict, real_col: str) -> str:
        """Return the canonical column name matching a real column name."""
        columns = profile.get("columns", {}) or {}
        if real_col in columns:
            return real_col
        for canonical, spec in columns.items():
            if real_col in (spec.get("aliases") or []):
                return canonical
        return real_col

    @staticmethod
    def _format_column_line(real_col: str, canonical_col: str, spec: dict) -> str:
        dtype = spec.get("dtype", "unknown")
        desc = spec.get("description", "")
        pending = spec.get("description_pending", False)
        parse_hint = spec.get("parse_hint")

        canonical_annot = (
            f" [canonical: {canonical_col}]"
            if real_col != canonical_col
            else ""
        )
        parse_annot = f" [parse: {parse_hint}]" if parse_hint else ""
        pending_annot = " [UNVERIFIED]" if pending else ""
        desc_str = f'"{desc}"' if desc else "(no description)"

        return (
            f"  - {real_col} ({dtype}){canonical_annot}: "
            f"{desc_str}{parse_annot}{pending_annot}"
        )
