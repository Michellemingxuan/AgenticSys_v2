"""RunItem → typed SSE event mapping for the orchestrator stream drive."""
from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from agents.items import MessageOutputItem, ToolCallItem, ToolCallOutputItem


def map_run_item(
    item,
    *,
    sess,
    turn_id,
    orch_t0: float,
    tool_calls: list[dict],
    call_index_by_id: dict[str, int],
    started_at_by_call: dict[str, int],
    team_plan_emitted: bool,
    first_tool_call_logged: bool,
    safe_dump: Callable,
    drain_specialist_errors: Callable[[], None],
) -> tuple[bool, bool, float | None]:
    """Translate one streamed ``RunItem`` into typed SSE events (``team_plan``
    / ``agent_started`` / ``agent_completed``).

    ``tool_calls`` / ``call_index_by_id`` / ``started_at_by_call`` are
    dicts/lists mutated in place (pass by reference). ``team_plan_emitted``
    and ``first_tool_call_logged`` are plain bools — pass the caller's
    current value in, get the (possibly updated) value back via the return
    tuple. ``safe_dump`` and ``drain_specialist_errors`` are the conductor's
    bound helper methods, threaded through as callables so this function
    stays free of any ``self``/conductor-instance coupling.

    Returns ``(team_plan_emitted, first_tool_call_logged,
    last_agent_completed_at)`` — ``last_agent_completed_at`` is the freshly
    stamped completion timestamp when ``item`` is a ``ToolCallOutputItem``,
    else ``None`` (the caller keeps its previously tracked value in that
    case; this mirrors the pre-extraction code, which only ever assigned the
    local inside that branch and read it via ``locals().get(...)`` after the
    stream-drain loop).
    """
    raw = getattr(item, "raw_item", None)
    last_agent_completed_at = None

    if isinstance(item, ToolCallItem):
        name = (
            getattr(raw, "name", None)
            or (raw.get("name") if isinstance(raw, dict) else None)
            or "?"
        )
        call_id = (
            getattr(raw, "call_id", None)
            or (raw.get("call_id") if isinstance(raw, dict) else None)
            or str(uuid.uuid4())
        )
        args_str = (
            getattr(raw, "arguments", None)
            or (raw.get("arguments") if isinstance(raw, dict) else "{}")
        )
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
        except json.JSONDecodeError:
            args = {"raw": args_str}
        sub_q = args.get("sub_question") or args.get("input") or json.dumps(args, default=str)

        call_index_by_id[call_id] = len(tool_calls)
        tool_calls.append({"call_id": call_id, "tool": name, "sub_question": sub_q})
        started_at_by_call[call_id] = int(time.time() * 1000)

        # The first tool call IS team construction — this is the
        # gap the user reports as "time to team construction stage".
        if not first_tool_call_logged:
            sess.logger.log("turn_phase_first_tool_call", {
                "turn_id": turn_id,
                "duration_ms_since_orch_start":
                    int((time.time() - orch_t0) * 1000),
                "first_tool": name,
            })
            first_tool_call_logged = True

        # First tool call → emit team_plan once (the orchestrator may add more
        # later; we send team_plan again on subsequent calls for incremental UX).
        team_plan_emitted = True
        sess.emit("team_plan", {"turn_id": turn_id, "tool_calls": list(tool_calls)})
        sess.emit("agent_started", {
            "turn_id": turn_id, "call_id": call_id, "tool": name,
            "started_at": started_at_by_call[call_id],
        })

    elif isinstance(item, ToolCallOutputItem):
        call_id = (
            getattr(raw, "call_id", None)
            or (raw.get("call_id") if isinstance(raw, dict) else None)
            or "?"
        )
        tool = "?"
        if call_id in call_index_by_id:
            tool = tool_calls[call_index_by_id[call_id]]["tool"]
        payload = safe_dump(item.output)
        started_ts = started_at_by_call.get(call_id, int(time.time() * 1000))
        duration_ms = int(time.time() * 1000) - started_ts
        # Stash the payload back onto `tool_calls` so a late-stage
        # orchestrator failure (ModelBehaviorError on FinalAnswer
        # parsing, etc.) can still synthesize a partial fallback
        # answer from the specialists' outputs the reviewer paid for.
        if call_id in call_index_by_id:
            tool_calls[call_index_by_id[call_id]]["payload"] = payload
            tool_calls[call_index_by_id[call_id]]["duration_ms"] = duration_ms
        sess.emit("agent_completed", {
            "turn_id": turn_id, "call_id": call_id, "tool": tool,
            "payload": payload, "duration_ms": duration_ms,
        })
        # If the agent_tool wrapper recorded a failure for any
        # specialist this run, fan out typed `error` events now so
        # the UI can show the real cause beside the vague `[FAILED …]`
        # payload it just received.
        drain_specialist_errors()
        # Stamp the time of the LAST agent_completed so we can
        # attribute the gap-to-end-of-stream to synthesis.
        last_agent_completed_at = time.time()

    elif isinstance(item, MessageOutputItem):
        pass  # handled by .final_output below

    return team_plan_emitted, first_tool_call_logged, last_agent_completed_at
