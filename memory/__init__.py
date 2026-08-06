"""AgenticSys ↔ Amem integration glue (config, factory, scope, IO helpers)."""
from .brief import build_session_brief
from .config import AmemConfig
from .factory import build_amem_manager
from .null_manager import NullAmemManager
from .loader import (
    ACTIVE_KP_KEEP,
    ACTIVE_KP_THRESHOLD,
    kp_seq,
    load_active_kps,
    load_case_kps,
    max_kp_seq,
    merge_recent_kps,
)
from .reader import load_case_summary
from .rewind import delete_case_memory, delete_turns
from .scope import base_metadata, build_scope, kps_for_agent_turn, kps_for_turn
from .writer import (
    consolidate_agent_case,
    consolidate_case,
    write_conversation,
    write_specialist_memory,
)

__all__ = [
    "AmemConfig", "build_amem_manager", "NullAmemManager",
    "build_scope", "base_metadata", "kps_for_turn", "kps_for_agent_turn",
    "write_conversation", "consolidate_case", "consolidate_agent_case",
    "write_specialist_memory", "load_case_kps", "load_active_kps",
    "merge_recent_kps", "kp_seq", "max_kp_seq",
    "ACTIVE_KP_THRESHOLD", "ACTIVE_KP_KEEP",
    "load_case_summary", "delete_turns", "delete_case_memory", "build_session_brief",
]
