"""Per-request context object threaded through Runner.run for tools."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AppContext:
    gateway: Any
    case_folder: Path
    logger: Any
