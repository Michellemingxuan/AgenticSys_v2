from unittest.mock import MagicMock
from openai import AsyncOpenAI
from llm.factory import build_session_clients
from llm.firewall_stack import FirewallStack
from logger.event_logger import EventLogger
from orchestrator.orchestrator import Orchestrator


def test_orchestrator_constructs_agent_graph():
    logger = EventLogger(session_id="t")
    firewall = FirewallStack(logger, max_retries=2, concurrency_cap=4)
    clients = build_session_clients(firewall, base_client=MagicMock(spec=AsyncOpenAI))

    orch = Orchestrator(
        llm=None,                    # legacy field tolerated; new path uses clients
        logger=logger,
        registry=None,
        pillar="credit",
        pillar_config={},
        catalog=MagicMock(),
        gateway=MagicMock(),
        clients=clients,
    )
    assert orch.orchestrator_agent is not None
    assert orch.orchestrator_agent.name == "orchestrator"
