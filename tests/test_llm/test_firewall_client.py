import pytest
from unittest.mock import AsyncMock
from llm.firewall_client import FirewalledAsyncOpenAI
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger

@pytest.mark.asyncio
async def test_outbound_messages_are_redacted():
    base = AsyncMock()
    base.chat.completions.create = AsyncMock(return_value="fake")
    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = FirewalledAsyncOpenAI(base=base, firewall=firewall)

    await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an assistant."},
            {"role": "user", "content": "Look up CASE-12345 and acct 1234567"},
        ],
    )

    sent = base.chat.completions.create.call_args.kwargs["messages"]
    assert sent[1]["content"] == "Look up [CASE-ID] and acct ***MASKED***"
