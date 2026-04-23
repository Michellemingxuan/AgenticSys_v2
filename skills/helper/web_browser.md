---
name: Web Browser
description: Fetch a short text excerpt from a public URL — available as an LLM-callable tool when an agent needs external context
type: helper
owner: [chat_agent, guardrail_agent, base_specialist]
mode: tool
tool_signature: "web_browser(url: str) -> str"
inputs:
  url: str
outputs:
  content: str
---

# Purpose

Fetch the visible text of a URL so the calling agent can quote or summarize it. Primarily useful when a reviewer asks about industry context, regulations, or definitions that are not in the case folder or in Acropedia.

# When to call

- Reviewer asks "what does the CFPB guidance on X say?" — fetch the cited URL.
- A report references an external URL — fetch it to verify or summarize.
- Rare: the agent needs a quick definition-lookup that `acropedia_lookup` does not cover.

# When NOT to call

- Generic fact questions that the LLM already knows ("what is DTI?") — prefer `acropedia_lookup` first.
- URLs that look like internal / private endpoints — this tool is for public web only.
- If a reviewer's question is off-topic, the Guardrail Agent should reject upstream; do not fetch from web to "help" an off-topic question.

# Output contract

Returns a plain-text excerpt (first ~1000 characters of visible body). Never fabricate a response; if the fetch fails, return a clear error string the LLM can surface to the reviewer.

# Status

Placeholder stub. The real adapter will land when the web-fetch infrastructure is wired in. Until then, callers receive a deterministic "web browser not yet available" response so skill wiring can be tested end-to-end.
