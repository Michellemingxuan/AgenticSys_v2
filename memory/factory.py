"""Backend-aware AmemManager construction with a store health check. Any failure
(build error, unreachable Qdrant) degrades to NullAmemManager so the app runs
exactly as it does today."""
from __future__ import annotations

from .config import AmemConfig
from .null_manager import NullAmemManager

# Imported at module level so tests can monkeypatch them.
try:  # pragma: no cover - import guard
    from Amem.integrations import (
        OpenAIConfig,
        SafeChainConfig,
        create_openai_manager,
        create_safechain_manager,
    )
except Exception:  # pragma: no cover
    OpenAIConfig = SafeChainConfig = None
    create_openai_manager = create_safechain_manager = None


def _log(logger, event: str, payload: dict) -> None:
    if logger is not None:
        try:
            logger.log(event, payload)
        except Exception:
            pass


def _accepts(fn, param: str) -> bool:
    """Whether `fn` takes `param`. Amem and this repo version independently, so
    a kwarg the installed Amem predates must not take memory down entirely —
    the outer `except` would swallow it into a NullAmemManager and silently
    disable memory."""
    try:
        import inspect
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def build_amem_manager(cfg: AmemConfig, *, backend: str, logger=None,
                       client=None):
    """Build the Amem manager.

    `client` is an ``AsyncOpenAI``-compatible client for Amem's OWN
    language-model calls, and it matters more than it looks: this repo calls
    `aupsert_case_memory` WITHOUT `content`, and Amem's manager does
    `summary = content or await self._asummarize_case(...)`. So every case
    consolidation runs an LLM call inside Amem — on case questions and answers.
    Left to build its own plain `AsyncOpenAI`, those calls skip the redaction
    and concurrency gating every other model call in this system goes through.

    Passing `FirewalledAsyncOpenAI` routes them back through the firewall.
    Chat is wrapped; `.embeddings` falls through its `__getattr__` to the base
    client, so one client covers both paths.

    SAFECHAIN IS DELIBERATELY LEFT ALONE — this is a documented exception to
    the openai/safechain parity rule, not an unfinished half:

      - `create_safechain_manager` takes no client. It takes
        `language_model_factory: Callable[[SafeChainConfig], Any]`, which Amem
        invokes PER REQUEST to build a fresh model (SafeChain clients expire),
        returning an object exposing
        `acomplete(prompt, *, purpose, metadata) -> str`.
      - Compliance is already satisfied: SafeChain's own redaction covers
        memory writes, so v2's redaction pass is not additionally required.
      - Gating is deliberately NOT shared. Amem and this system are independent
        — Amem stores QA/KPs and reloads what it needs. Routing its synthesis
        through v2's semaphores would gate end-of-turn consolidation behind
        live specialist capacity for no compliance benefit.
    """
    if not cfg.enabled:
        _log(logger, "amem_disabled", {"reason": "AMEM_ENABLED=0"})
        return NullAmemManager()
    try:
        if backend == "safechain":
            manager = create_safechain_manager(
                cfg.store_url, config=SafeChainConfig(dimensions=cfg.vector_size))
        else:
            kwargs = {}
            if client is not None and _accepts(create_openai_manager, "client"):
                kwargs["client"] = client
            elif client is not None:
                _log(logger, "amem_client_not_forwarded", {
                    "reason": "installed Amem's create_openai_manager takes no "
                              "`client`; its LLM calls will NOT be firewalled",
                })
            manager = create_openai_manager(
                cfg.store_url, config=OpenAIConfig(dimensions=cfg.vector_size),
                **kwargs)
        # Health check: a cheap round-trip to the store.
        manager.list_memories(limit=1)
        _log(logger, "amem_ready", {
            "backend": backend, "store_url": cfg.store_url,
            # Visible in the boot log: whether Amem's own LLM calls are gated.
            "llm_calls_firewalled": bool(client) and backend != "safechain",
        })
        return manager
    except Exception as exc:
        _log(logger, "amem_unavailable",
             {"backend": backend, "store_url": cfg.store_url, "error": repr(exc)})
        return NullAmemManager()
