"""Per-request context object threaded through Runner.run for tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AppContext:
    gateway: Any
    case_folder: Path
    logger: Any
    # Per-specialist conversation history, keyed by tool name (== specialist
    # name as registered on the orchestrator). The redacting_tool wrapper
    # reads this on each invocation: when a prior history exists for the
    # specialist, the sub-agent is run with that history prepended to the
    # new sub-question, so follow-up calls see what was already asked /
    # answered. After each sub-agent run finishes, the wrapper saves
    # `result.to_input_list()` back here. Reset is per-AppContext: re-running
    # the cell that constructs a fresh AppContext starts a fresh chain.
    _specialist_histories: dict[str, list] = field(default_factory=dict)
