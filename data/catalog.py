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

    def to_prompt_context(self) -> str:
        """Format the full catalog as text for injection into LLM prompts.

        This is what specialists see when they need to understand what data
        is available and what each column means.
        """
        lines: list[str] = ["=== DATA CATALOG ===", ""]
        for table in self.list_tables():
            desc = self.get_description(table)
            lines.append(f"TABLE: {table}")
            lines.append(f"  {desc}")
            details = self.get_column_details(table)
            if details:
                for col, info in details.items():
                    col_desc = info.get("description", "")
                    col_type = info["type"]
                    extras = []
                    if "values" in info:
                        extras.append(f"values: {', '.join(info['values'])}")
                    elif "min" in info and "max" in info:
                        extras.append(f"range: {info['min']}–{info['max']}")
                    extra_str = f" ({', '.join(extras)})" if extras else ""
                    lines.append(f"  - {col} [{col_type}]{extra_str}: {col_desc}")
            lines.append("")
        return "\n".join(lines)
