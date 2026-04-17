"""Data catalog backed by YAML profile definitions."""

from __future__ import annotations

from pathlib import Path

import yaml


class DataCatalog:
    """Loads table metadata from YAML data-profile configs."""

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

    def to_prompt_context(self) -> str:
        lines: list[str] = []
        for table in self.list_tables():
            desc = self.get_description(table)
            lines.append(f"- {table}: {desc}")
            schema = self.get_schema(table)
            if schema:
                for col, info in schema.items():
                    lines.append(f"    {col} ({info['type']})")
        return "\n".join(lines)
