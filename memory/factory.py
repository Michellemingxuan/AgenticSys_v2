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


def build_amem_manager(cfg: AmemConfig, *, backend: str, logger=None):
    if not cfg.enabled:
        _log(logger, "amem_disabled", {"reason": "AMEM_ENABLED=0"})
        return NullAmemManager()
    try:
        if backend == "safechain":
            manager = create_safechain_manager(
                cfg.store_url, config=SafeChainConfig(dimensions=cfg.vector_size))
        else:
            manager = create_openai_manager(
                cfg.store_url, config=OpenAIConfig(dimensions=cfg.vector_size))
        # Health check: a cheap round-trip to the store.
        manager.list_memories(limit=1)
        _log(logger, "amem_ready", {"backend": backend, "store_url": cfg.store_url})
        return manager
    except Exception as exc:
        _log(logger, "amem_unavailable",
             {"backend": backend, "store_url": cfg.store_url, "error": repr(exc)})
        return NullAmemManager()
