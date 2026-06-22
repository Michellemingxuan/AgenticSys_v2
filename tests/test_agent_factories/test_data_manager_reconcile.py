# tests/test_agent_factories/test_data_manager_reconcile.py
import json
import pytest
from agent_factories.data_manager_agent import DataManagerAgent


class _FakeLLM:
    """Mirrors the real FirewalledChatShim contract:
    ainvoke(system_prompt, user_message, **kwargs) -> object with
    .status="success" and .data={"response": <text>}.
    """
    def __init__(self, reply):
        self._reply = reply

    async def ainvoke(self, system_prompt, user_message, **kwargs):
        return type("LLMResult", (), {
            "status": "success",
            "data": {"response": self._reply},
        })()


def _agent(reply):
    return DataManagerAgent(gateway=None, catalog=None, llm=_FakeLLM(reply),
                            logger=type("L", (), {"log": lambda *a, **k: None})())


@pytest.mark.asyncio
async def test_match_column_returns_choice_and_confidence():
    agent = _agent(json.dumps({"canonical_col": "credit_loss_prob", "confidence": 0.91}))
    out = await agent.match_column("cust_cdss_score", ["100", "55"], ["credit_loss_prob", "cbr_score"])
    assert out == {"canonical_col": "credit_loss_prob", "confidence": 0.91}


@pytest.mark.asyncio
async def test_normalize_threshold_text_never_invents_value():
    # Agent reply claims a value NOT present in the source text -> rejected -> None.
    agent = _agent(json.dumps({"risk_threshold": 999.0, "risk_direction": "above"}))
    out = await agent.normalize_threshold_text("clearly above 5.8 is risky")
    assert out is None  # 999 not found in source -> invariant guard rejects it


@pytest.mark.asyncio
async def test_polish_description_passthrough_when_no_llm():
    agent = DataManagerAgent(gateway=None, catalog=None, llm=None,
                             logger=type("L", (), {"log": lambda *a, **k: None})())
    assert await agent.polish_description("x", "raw text", "brief") == "raw text"


@pytest.mark.asyncio
async def test_match_column_none_llm_returns_default():
    """Degraded path: no LLM -> canonical_col=None, confidence=0.0."""
    agent = DataManagerAgent(gateway=None, catalog=None, llm=None,
                             logger=type("L", (), {"log": lambda *a, **k: None})())
    out = await agent.match_column("some_col", ["a", "b"], ["canon_col"])
    assert out == {"canonical_col": None, "confidence": 0.0}


@pytest.mark.asyncio
async def test_normalize_threshold_text_none_llm_returns_none():
    """Degraded path: no LLM -> None."""
    agent = DataManagerAgent(gateway=None, catalog=None, llm=None,
                             logger=type("L", (), {"log": lambda *a, **k: None})())
    out = await agent.normalize_threshold_text("above 5.8 is risky")
    assert out is None
