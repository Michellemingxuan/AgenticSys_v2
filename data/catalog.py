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
