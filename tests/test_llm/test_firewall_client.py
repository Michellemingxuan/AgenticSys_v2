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


from llm.firewall_stack import FirewallRejection, FIREWALL_GUIDANCE


@pytest.mark.asyncio
async def test_retry_with_guidance_on_firewall_rejection():
    base = AsyncMock()
    # First call raises FirewallRejection, second call succeeds.
    base.chat.completions.create = AsyncMock(side_effect=[
        FirewallRejection("PII", "blocked"),
        "ok",
    ])
    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = FirewalledAsyncOpenAI(base=base, firewall=firewall)

    result = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Original system prompt."},
            {"role": "user", "content": "user input"},
        ],
    )

    assert result == "ok"
    # Second call's system prompt has guidance appended.
    second_messages = base.chat.completions.create.call_args_list[1].kwargs["messages"]
    assert FIREWALL_GUIDANCE in second_messages[0]["content"]


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    base = AsyncMock()
    base.chat.completions.create = AsyncMock(side_effect=FirewallRejection("PII", "always"))
    firewall = FirewallStack(EventLogger(session_id="t"), max_retries=2, concurrency_cap=4)
    client = FirewalledAsyncOpenAI(base=base, firewall=firewall)

    with pytest.raises(FirewallRejection):
        await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )
    # 1 original + 2 retries = 3 attempts
    assert base.chat.completions.create.call_count == 3
