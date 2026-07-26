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
