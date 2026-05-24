"""Per-node trace package: SQLite-backed observability for every LLM call boundary."""
from tools.node_trace.core import (
    ACTIVE_NODE,
    NodeTrace,
    NodeTraceStore,
    TURN_SCOPE,
    TurnScope,
    attach_extra,
    attach_io,
    attach_latency,
    attach_tag,
    attach_usage,
    current_turn_scope,
    _hooks_own_rounds,
    _open_node,
)
from tools.node_trace.hooks import NodeTraceRunHooks

__all__ = [
    "ACTIVE_NODE",
    "NodeTrace",
    "NodeTraceStore",
    "NodeTraceRunHooks",
    "TURN_SCOPE",
    "TurnScope",
    "attach_extra",
    "attach_io",
    "attach_latency",
    "attach_tag",
    "attach_usage",
    "current_turn_scope",
    "_open_node",
]
