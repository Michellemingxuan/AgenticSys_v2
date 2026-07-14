"""SDK lifecycle hook that creates a NodeTrace child row per LLM call.

Why this exists: the openai-agents SDK's ``Runner.run_streamed`` runs the
actual ``chat.completions.create`` in a background asyncio task whose
context doesn't carry our ``ACTIVE_NODE`` contextvar. The auto-round-
creation in ``firewall_client._FirewalledChatCompletions.create`` (which
relies on ACTIVE_NODE) therefore never fires for the streamed orchestrator
path, so we never get ``orchestrator.round_N`` rows.

The SDK ``RunHooks`` callbacks (``on_llm_start`` / ``on_llm_end``) fire
synchronously on the same path as the LLM call, regardless of which task
is driving the stream. We bind the parent NodeTrace at hook construction
time so we don't depend on contextvar propagation at all.

Usage:

    async with _open_node(store, "orchestrator", depth=0) as orch_nt:
        streamed = Runner.run_streamed(
            agent, run_input, context=ctx,
            hooks=NodeTraceRunHooks(store, orch_nt),
        )
        async for event in streamed.stream_events(): ...

Works for the non-streamed ``Runner.run`` path too — and would be the
clean way to capture specialist rounds as well, replacing the contextvar-
based path. Kept separate for now so the migration is incremental.
"""
from __future__ import annotations

import json
import os
from time import perf_counter
from typing import Any  # noqa: F401 — used in extra: dict[str, Any] below

from agents import RunHooks

from tools.node_trace.core import (
    NodeTrace,
    NodeTraceStore,
    _excerpt,
    _now_iso,
)
from tools.node_trace.pricing import compute_cost


def _estimate_tokens(text: str, model: str | None) -> int:
    """tiktoken token estimate for backends that return NO usage object (e.g.
    safechain, which explicitly doesn't). Approximate — rows built this way are
    flagged ``tokens_estimated`` so the count is never mistaken for exact. Falls
    back to a chars/4 heuristic if tiktoken is unavailable or the model name
    isn't recognized."""
    if not text:
        return 0
    try:
        import tiktoken
        try:
            enc = (tiktoken.encoding_for_model(model) if model
                   else tiktoken.get_encoding("cl100k_base"))
        except Exception:  # noqa: BLE001 — unknown model repr → generic encoding
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001 — tiktoken missing → rough heuristic
        return max(1, len(text) // 4)


def _serialize_items(items: list[Any]) -> list[dict]:
    out: list[dict] = []
    for it in items or []:
        if isinstance(it, dict):
            out.append(it)
            continue
        if hasattr(it, "model_dump"):
            try:
                out.append(it.model_dump())
                continue
            except Exception:  # noqa: BLE001
                pass
        # Last-resort: stringify
        try:
            out.append(json.loads(json.dumps(it, default=str)))
        except Exception:  # noqa: BLE001
            out.append({"_unserializable": str(it)[:200]})
    return out


def _io_cap_bytes() -> int:
    try:
        return int(os.environ.get("NODE_TRACE_MAX_IO_BYTES", "1048576"))
    except ValueError:
        return 1_048_576


class NodeTraceRunHooks(RunHooks):
    """Creates a child NodeTrace row per LLM call inside a Runner.

    Bind the parent NodeTrace at construction time so we don't depend on
    contextvar propagation through the SDK's background streaming task.

    Uses a STACK of in-flight rounds keyed by start event, not a single
    shared row_id. The openai-agents SDK can fire ``on_llm_start`` twice
    in rapid succession for the same underlying LLM call (once for the
    streamed wrapper, once for the inner call). A single-field hook would
    have the second start clobber the first's row_id, and the matching
    on_llm_end would then update the wrong row. The stack ensures each
    start/end pair is matched 1:1 in LIFO order.
    """

    def __init__(
        self,
        store: NodeTraceStore | None,
        parent: NodeTrace | None,
    ) -> None:
        self._store = store
        self._parent = parent
        # Stack of (row_id, t0_perf) tuples for in-flight LLM calls.
        # LIFO matching: the most recent on_llm_start gets paired with
        # the next on_llm_end.
        self._inflight: list[tuple[int, float, str]] = []
        self._enabled = (
            store is not None
            and isinstance(parent, NodeTrace)
            and parent.row_id > 0
        )
        # Flag the parent so firewall_client knows hooks are creating
        # round children — it stops also wrapping each LLM call (which
        # would double-insert rows and leave a partial-data row per
        # call, since streamed responses can't be model_dump_json'd).
        if self._enabled:
            self._parent._hooks_own_rounds = True  # type: ignore[union-attr]

    async def on_llm_start(
        self, context, agent, system_prompt, input_items,
    ) -> None:
        if not self._enabled:
            return
        try:
            round_idx = self._parent.next_round_index()  # type: ignore[union-attr]
            node_name = f"{self._parent.node}.round_{round_idx}"  # type: ignore[union-attr]
            t0 = perf_counter()
            row_id = self._store.insert(  # type: ignore[union-attr]
                chat_id=self._parent.chat_id,        # type: ignore[union-attr]
                case_id=self._parent.case_id,        # type: ignore[union-attr]
                turn_id=self._parent.turn_id,        # type: ignore[union-attr]
                node=node_name,
                parent_id=self._parent.row_id,       # type: ignore[union-attr]
                depth=self._parent.depth + 1,        # type: ignore[union-attr]
                started_at=_now_iso(),
                model=str(agent.model) if getattr(agent, "model", None) else None,
            )
            # Build the messages payload for the input excerpt + full I/O, and
            # stash it so on_llm_end can estimate prompt tokens when the backend
            # returns no usage object (e.g. safechain).
            sys = [{"role": "system", "content": system_prompt}] if system_prompt else []
            payload = sys + _serialize_items(input_items)
            payload_json = json.dumps(payload, default=str)
            self._inflight.append((row_id, t0, payload_json))
            cap = _io_cap_bytes()
            self._store.update(  # type: ignore[union-attr]
                row_id,
                messages_json=(
                    payload_json if len(payload_json) <= cap
                    else f'{{"_elided": "payload >{cap}B", "size": {len(payload_json)}}}'
                ),
                prompt_excerpt=_excerpt(payload_json),
                prompt_chars=len(payload_json),
                system_prompt_chars=len(system_prompt) if system_prompt else None,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break the LLM call
            pass

    async def on_llm_end(self, context, agent, response) -> None:
        if not self._enabled or not self._inflight:
            return
        # LIFO pair: the most recent on_llm_start matches this on_llm_end.
        # Matches the SDK's synchronous start→end nesting.
        row_id, t0, prompt_text = self._inflight.pop()
        try:
            duration_ms = int((perf_counter() - t0) * 1000)
            # Usage extraction. The SDK's ModelResponse may carry the
            # OpenAI usage object on its `usage` attr; field names match
            # the Responses API (`input_tokens` / `output_tokens`) on
            # newer SDK versions, the older chat-completion shape
            # (`prompt_tokens` / `completion_tokens`) on older ones.
            # Probe both.
            p_tok = c_tok = t_tok = cached = reasoning = None
            usage = getattr(response, "usage", None)
            if usage is not None:
                p_tok = (
                    getattr(usage, "input_tokens", None)
                    or getattr(usage, "prompt_tokens", None)
                )
                c_tok = (
                    getattr(usage, "output_tokens", None)
                    or getattr(usage, "completion_tokens", None)
                )
                t_tok = getattr(usage, "total_tokens", None)
                pdet = (
                    getattr(usage, "input_tokens_details", None)
                    or getattr(usage, "prompt_tokens_details", None)
                )
                if pdet is not None:
                    cached = (
                        pdet.get("cached_tokens") if isinstance(pdet, dict)
                        else getattr(pdet, "cached_tokens", None)
                    )
                cdet = (
                    getattr(usage, "output_tokens_details", None)
                    or getattr(usage, "completion_tokens_details", None)
                )
                if cdet is not None:
                    reasoning = (
                        cdet.get("reasoning_tokens") if isinstance(cdet, dict)
                        else getattr(cdet, "reasoning_tokens", None)
                    )
            model = str(agent.model) if getattr(agent, "model", None) else None
            # Output capture: the response carries the items the model
            # produced. Serialize them in the same shape as a chat-completion
            # output_json so the viewer's tool-call extractor still works.
            try:
                out_items = (
                    response.to_input_items()
                    if hasattr(response, "to_input_items")
                    else []
                )
            except Exception as exc:  # noqa: BLE001
                out_items = []
            out_payload = _items_to_chatcompletion_shape(out_items)
            out_json = json.dumps(out_payload, default=str) if out_payload else ""
            # No usage object (e.g. safechain, which doesn't return one) →
            # estimate tokens from the stashed input + the captured output so the
            # row still carries counts. Flagged `tokens_estimated`; the OpenAI
            # path (usage present) is untouched. `cached_input_tokens` can't be
            # inferred without real usage, so it stays null (no cache rate here).
            tokens_estimated = False
            if usage is None:
                p_tok = _estimate_tokens(prompt_text, model)
                c_tok = _estimate_tokens(out_json, model)
                t_tok = p_tok + c_tok
                tokens_estimated = True
            cost = compute_cost(
                model=model,
                prompt_tokens=p_tok,
                cached_input_tokens=cached,
                completion_tokens=c_tok,
            )
            # Diagnostic: ALWAYS stash the raw response shape so we can
            # see exactly what the SDK handed us — empty output rounds
            # become inspectable via the Extras tab regardless of whether
            # the packager recognized the items.
            extra: dict[str, Any] = {
                "response_type": type(response).__name__,
                "agent_model_repr": repr(getattr(agent, "model", None))[:200],
                "response_id": getattr(response, "response_id", None),
                "n_out_items": len(out_items),
                "out_item_types": [
                    (it.get("type") if isinstance(it, dict)
                     else type(it).__name__)
                    for it in (out_items or [])[:10]
                ],
                "usage_repr": repr(usage)[:300] if usage is not None else None,
                "packager_recognized": bool(out_payload),
                "tokens_estimated": tokens_estimated,
            }
            if not out_items:
                extra["note"] = (
                    "response.output was empty — typically an SDK "
                    "intermediate iteration"
                )
            elif not out_payload:
                extra["note"] = (
                    "items did not match function_call or assistant-message shapes"
                )
                extra["raw_output_items"] = out_items
            cap = _io_cap_bytes()
            self._store.update(  # type: ignore[union-attr]
                row_id,
                ended_at=_now_iso(),
                duration_ms=duration_ms,
                llm_call_ms=duration_ms,
                outcome="ok",
                model=model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                cached_input_tokens=cached,
                reasoning_tokens=reasoning,
                cost_usd=cost or None,
                output_json=(
                    out_json if 0 < len(out_json) <= cap
                    else (out_json if not out_json
                          else f'{{"_elided": "payload >{cap}B", "size": {len(out_json)}}}')
                ),
                completion_excerpt=_excerpt(out_json) if out_json else None,
                completion_chars=len(out_json) if out_json else None,
                extra_json=json.dumps(extra, default=str) if extra else None,
            )
        except Exception:  # noqa: BLE001
            pass

    # The hook protocol also wants these methods; we leave them no-op
    # so we don't open extra rows for non-LLM events (we already track
    # tools / agent boundaries via agent_tool's wraps).
    async def on_agent_start(self, context, agent) -> None: return
    async def on_agent_end(self, context, agent, output) -> None: return
    async def on_tool_start(self, context, agent, tool) -> None: return
    async def on_tool_end(self, context, agent, tool, result) -> None: return
    async def on_handoff(self, context, from_agent, to_agent) -> None: return


def _items_to_chatcompletion_shape(items: list[Any]) -> dict:
    """Repackage a list of ResponseItem-shaped outputs into a synthetic
    ChatCompletion-shaped dict so the viewer's existing tool-call
    extractor (which looks for ``choices[0].message.tool_calls``) keeps
    working uniformly for both the firewall_client path and the hook path.
    """
    if not items:
        return {}
    tool_calls = []
    content_pieces: list[str] = []
    for it in items:
        d = it if isinstance(it, dict) else (
            it.model_dump() if hasattr(it, "model_dump") else None
        )
        if not isinstance(d, dict):
            continue
        itype = d.get("type") or ""
        if itype == "function_call" or "function_call" in itype:
            tool_calls.append({
                "id": d.get("call_id") or d.get("id") or "",
                "type": "function",
                "function": {
                    "name": d.get("name") or "",
                    "arguments": d.get("arguments") or "{}",
                },
            })
        elif itype == "message" or "message" in itype or d.get("role") == "assistant":
            # Try common content shapes
            c = d.get("content")
            if isinstance(c, str):
                content_pieces.append(c)
            elif isinstance(c, list):
                for piece in c:
                    if isinstance(piece, dict):
                        txt = piece.get("text") or piece.get("content")
                        if isinstance(txt, str):
                            content_pieces.append(txt)
    msg: dict[str, Any] = {"role": "assistant"}
    if content_pieces:
        msg["content"] = "\n".join(content_pieces)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}
