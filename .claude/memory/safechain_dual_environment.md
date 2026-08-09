---
name: SafeChain in private environment, OpenAI API in dev
description: AgenticSys runs safechain in prod and the OpenAI API in dev; the safechain model is a real LangChain AzureChatOpenAI subclass, so the transport is native (measured, not assumed)
type: project
originSessionId: 797673ed-b57e-4e47-b7ca-2f717ff3fb69
---
Two LLM backends, one architecture:

- **Private (production)**: `safechain` — `from safechain.core.model import amodel`. Auth and content policy go through the SafeChain pipeline.
- **Local dev / this repo**: direct OpenAI API via `openai.AsyncOpenAI` + the `openai-agents` SDK.

**Why:** the private env restricts which providers can be reached and enforces content policy at the SafeChain layer.

## What the safechain build actually supports

Measured in the private env with `tools/safechain_probe.py` — re-run it to re-verify and replace this section when the build changes. **Do not infer these from v1 or from the code; they were wrong for over a year and shaped a whole subsystem before anyone measured.**

Build at time of measurement (2026-07-29): `safechain.__version__` unset, `langchain_core 1.4.0`, model id `gpt-4.1`.

`await amodel(...)` returns a **real LangChain chat model**:

```
SafeAzureChatOpenAI <- InputRedactor <- OpenAIMiddleware <- AzureChatOpenAI <- BaseChatOpenAI
```

Because it subclasses `AzureChatOpenAI`, the standard surface works natively:

- **`.bind(**openai_shaped_kwargs)` passes straight through to the Azure API** — the SDK's own `tools` / `tool_choice` / `response_format` payload is forwarded verbatim, no translation to LangChain objects.
- **`bind_tools` is native** — returns an `AIMessage` with populated `.tool_calls`.
- **`tools` + `response_format` together works**, including prod's actual pairing (strict tools from `@function_tool`, non-strict schema from `AgentOutputSchema(..., strict_json_schema=False)`). `.content` comes back as the **raw JSON string**.
  - Use **`.bind(response_format=…)`, NOT `.with_structured_output(…)`**.
  - **EVERY TOOL MUST BE STRICT. The RESPONSE schema may be non-strict; the TOOLS may not.** This is the sharp edge in the line above, and the probe missed it because it only ever bound strict tools. Hit in prod 2026-08-10:

    ```
    ValueError: 'make_chart' is not strict. Only 'strict' function can be auto-parsed
    ```

    Dev cannot reproduce it: the openai path calls `chat.completions.create`, which does not validate tool strictness. Prod does, so ONE non-strict tool fails the whole turn — and every specialist round carries `response_format`, so it is every turn.

    Fixed by making both offenders strict (`e36ab6f`). `get_chart_guidance` took no parameters at all and its `strict_mode=False` was gratuitous; `make_chart` was genuinely blocked by `points: list[dict]`, since a strict schema rejects an open-ended object array — it now takes `points_json: str`, the same JSON-string shape `query_table(filters=…)` and `batch_aggregate(specs_json=…)` already use for this exact constraint.

    **A test now audits all 18 specialist tools for strictness**, so a future `strict_mode=False` fails in dev instead of surfacing here. If you ever genuinely need a non-strict tool in prod, the parameter must become a JSON string — that is the only shape that survives.

  - MECHANISM NOT PROBED: the error implies langchain routes a bound `response_format` through OpenAI's auto-parse (`validate_input_tools`), not just `.with_structured_output(…)` as this file previously said. That is INFERRED FROM THE ERROR TEXT, not measured — re-run `tools/safechain_probe.py` with a deliberately non-strict tool if you want it confirmed rather than assumed.
- **`tool_choice="required"` is enforced natively** (also the named-function form). `"any"` is invalid on Azure — only `none` / `auto` / `required`.
- **`parallel_tool_calls=True` works.**
- **Streaming is real** — `astream` yields `AIMessageChunk`s carrying `tool_call_chunks`.
- **Multi-message role-tagged input works.**
- `safechain.lcel` is retry helpers (`LCELRetry`, `retry`), NOT a model factory.

## Where compliance lives

The two layers do different jobs — "keep the firewall template or go native" was a false choice:

- **`InputRedactor` is in the MODEL's MRO** and redacts message-list inputs (`_redact_chat_input`, `_redact_base_message_input`, …). Content redaction applies on a direct `.ainvoke(messages)`; it does NOT depend on the prompt template.
- **`ValidChatPromptTemplate` is a thin `ChatPromptTemplate` subclass** whose only meaningful override is `format_prompt` — template-time validation, and the one thing a bare `.ainvoke(messages)` would skip.
- **`OpenAIMiddleware`** overrides `validate` and owns token refresh (`_arefresh_token_if_expired`) — auth, not content policy.

So the shipping shape keeps both (verified end to end, probe check `R3`):

```python
ValidChatPromptTemplate.from_messages([MessagesPlaceholder("messages")]) | model.bind(**openai_kwargs)
```

## Still true, still current

- Auth: HTTP `401` → token expiry → refresh the model and retry once. `403` / `400` → `FirewallRejection`. (`OpenAIMiddleware` may already refresh on 401, making the client's retry redundant — unverified, kept as belt-and-braces.)
- Pre-sanitization: case-ID scrub → digit mask (`\b\d{8,}\b → ***MASKED***`) → exec-keyword filter.
- Neutral role labels ("Context"/"Request"/"Response") were a v1 firewall workaround. No longer used — real roles are sent now, and nothing broke.

## Architecture stays identical in both environments

Only the transport varies. `Agent`, `Runner.run`, agent factories, instruction composition, `EventLogger` — identical. Only `build_session_clients(backend=...)` dispatches: `"openai"` → `FirewalledAsyncOpenAI`; `"safechain"` → `SafeChainAsyncOpenAI`, which mimics `AsyncOpenAI` at the **HTTP-client boundary** so `OpenAIChatCompletionsModel` does all the SDK-internals work unchanged.

`llm/safechain_client.py` converts OpenAI-wire messages to LangChain messages, binds the SDK's kwargs, and converts the `AIMessage` back to a `ChatCompletion`. safechain imports are lazy so dev tests still collect. See [[feedback_openai_safechain_parity]] before changing either transport.

**Open item:** `runner/turn/conductor.py` still carries a no-tools-retry workaround that exists only because the old text protocol could not enforce `tool_choice="required"`. Native enforcement is now proven, so it can go — deliberately left in place until a prod run validates the rewrite.
