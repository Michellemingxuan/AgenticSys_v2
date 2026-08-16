"""Loader for pillar YAML configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class PillarLoader:
    """Loads and caches pillar configuration from YAML files."""

    def __init__(self, pillar_dir: str = "config/pillars"):
        self.pillar_dir = Path(pillar_dir)
        self._cache: dict[str, dict] = {}

    def load(self, pillar_name: str) -> dict | None:
        """Load a pillar config by name, returning cached copy if available.

        Returns None if the pillar YAML does not exist.
        """
        if pillar_name in self._cache:
            return self._cache[pillar_name]

        path = self.pillar_dir / f"{pillar_name}.yaml"
        if not path.exists():
            return None

        with open(path) as f:
            data = yaml.safe_load(f)

        self._cache[pillar_name] = data
        return data

    def list_pillars(self) -> list[str]:
        """Return sorted list of available pillar names (stem of each .yaml)."""
        if not self.pillar_dir.exists():
            return []
        return sorted(p.stem for p in self.pillar_dir.glob("*.yaml"))

    def get_specialist_config(self, pillar_name: str, domain: str) -> dict | None:
        """Return the specialist config dict for a given domain within a pillar.

        Returns None if the pillar or domain does not exist.

        This used to copy pillar-level fields down into the specialist config,
        which existed solely to carry `cut_off_date`. That constant is gone —
        the cut-off is derived per case by `tools.data_tools.case_cut_off` and
        delivered with the round-1 inventory, because one date per pillar was
        wrong for every case but one and could not be contradicted by the data.
        Re-adding `cut_off_date` to a pillar YAML will therefore do nothing.
        """
        pillar = self.load(pillar_name)
        if pillar is None:
            return None
        specialists = pillar.get("specialists", {})
        spec_config = specialists.get(domain)
        if spec_config is None:
            return None
        return dict(spec_config)
