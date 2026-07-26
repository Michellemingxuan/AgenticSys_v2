"""Deterministic conversation identity + per-process server-run id.

`conversation_id` is DERIVED, never minted: same (case, user, pillar) →
same id forever, across server restarts. That is what makes node_trace
restart-invisible with no persistence or lookup. `SERVER_RUN_ID` is a
diagnostic value, one per process — never a grouping key.
"""
from __future__ import annotations

import os
import uuid

_SEP = "::"


def compose_conversation_id(case_id: str, user_id: str, pillar_id: str) -> str:
    return f"{case_id}{_SEP}{user_id}{_SEP}{pillar_id}"


def resolve_user_id(cfg) -> str:
    uid = getattr(cfg, "user_id", None) if cfg else None
    return uid or os.environ.get("AMEM_USER_ID", "amx_reviewer")


# Minted once per process, at import.
SERVER_RUN_ID: str = f"run-{uuid.uuid4().hex[:8]}"
