from memory.config import AmemConfig
from memory.factory import build_amem_manager
from memory.null_manager import NullAmemManager

BASE = dict(store_url="http://127.0.0.1:6333", collection_name="c", vector_size=3072,
            read_timeout_s=1.5, write_timeout_s=5.0, retrieve_limit=6,
            org_id="amx", user_id="amx_reviewer")


def test_disabled_returns_null():
    cfg = AmemConfig(enabled=False, **BASE)
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, NullAmemManager)


def test_build_failure_falls_back_to_null(monkeypatch):
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    def boom(*a, **k):
        raise RuntimeError("qdrant unreachable")
    monkeypatch.setattr(f, "create_openai_manager", boom)
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, NullAmemManager)


def test_healthcheck_failure_falls_back(monkeypatch):
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    class HalfDead:
        def list_memories(self, **k):
            raise RuntimeError("store down")
        def close(self):
            pass
    monkeypatch.setattr(f, "create_openai_manager", lambda *a, **k: HalfDead())
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, NullAmemManager)


def test_healthy_manager_passthrough(monkeypatch):
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    class Healthy:
        def list_memories(self, **k):
            return []
    monkeypatch.setattr(f, "create_openai_manager", lambda *a, **k: Healthy())
    mgr = build_amem_manager(cfg, backend="openai")
    assert isinstance(mgr, Healthy)


# ── Amem's own LLM calls must go through the firewall ───────────────────────
#
# `aupsert_case_memory` is called without `content`, and Amem's manager does
# `summary = content or await self._asummarize_case(...)` — so consolidating a
# case runs an LLM call INSIDE Amem, over this case's questions and answers.
# Without a client it builds a plain AsyncOpenAI and skips redaction entirely.


def test_the_firewalled_client_is_forwarded_to_amem(monkeypatch):
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    seen = {}

    def _capture(store_url, *, config=None, client=None, **kw):
        seen["client"] = client
        return type("Healthy", (), {"list_memories": lambda self, **k: []})()

    monkeypatch.setattr(f, "create_openai_manager", _capture)
    sentinel = object()
    build_amem_manager(cfg, backend="openai", client=sentinel)
    assert seen["client"] is sentinel


def test_an_amem_without_client_support_still_builds(monkeypatch):
    """Amem and this repo version independently. A kwarg the installed Amem
    predates must not fall into the outer except and silently disable memory."""
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    def _old_signature(store_url, *, config=None):      # no `client`
        return type("Healthy", (), {"list_memories": lambda self, **k: []})()

    monkeypatch.setattr(f, "create_openai_manager", _old_signature)
    mgr = build_amem_manager(cfg, backend="openai", client=object())
    assert not isinstance(mgr, NullAmemManager), "memory must not silently die"


def test_safechain_ignores_the_client(monkeypatch):
    """SafeChain builds its models from factories and takes no client."""
    cfg = AmemConfig(enabled=True, **BASE)
    import memory.factory as f

    seen = {}

    def _capture(store_url, *, config=None, **kw):
        seen.update(kw)
        return type("Healthy", (), {"list_memories": lambda self, **k: []})()

    monkeypatch.setattr(f, "create_safechain_manager", _capture)
    build_amem_manager(cfg, backend="safechain", client=object())
    assert "client" not in seen
