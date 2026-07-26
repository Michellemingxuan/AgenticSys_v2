"""AgenticSys ↔ Amem integration glue (config, factory, scope, IO helpers)."""
from .brief import build_session_brief
from .config import AmemConfig
from .factory import build_amem_manager
from .null_manager import NullAmemManager
from .loader import load_case_kps
from .reader import retrieve_context, search_kp
from .rewind import delete_case_memory, delete_turns
from .scope import base_metadata, build_scope, kps_for_agent_turn, kps_for_turn
from .writer import (
    consolidate_case,
    mirror_kp_working,
    write_conversation,
    write_specialist_memory,
)

__all__ = [
    "AmemConfig", "build_amem_manager", "NullAmemManager",
    "build_scope", "base_metadata", "kps_for_turn", "kps_for_agent_turn",
    "mirror_kp_working", "write_conversation", "consolidate_case",
    "write_specialist_memory", "load_case_kps",
    "retrieve_context", "search_kp", "delete_turns", "delete_case_memory", "build_session_brief",
]
