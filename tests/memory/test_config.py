import importlib
from memory.config import AmemConfig


def test_defaults_when_env_absent(monkeypatch):
    for k in ("AMEM_ENABLED", "AMEM_STORE_URL", "AMEM_VECTOR_SIZE",
              "AMEM_READ_TIMEOUT_S", "AMEM_RETRIEVE_LIMIT",
              "AMEM_ORG_ID", "AMEM_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    cfg = AmemConfig.from_env()
    assert cfg.enabled is True
    assert cfg.store_url == "http://127.0.0.1:6333"
    assert cfg.vector_size == 3072
    assert cfg.read_timeout_s == 1.5
    assert cfg.retrieve_limit == 6
    assert cfg.org_id == "amx"
    assert cfg.user_id == "amx_reviewer"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("AMEM_ENABLED", "0")
    monkeypatch.setenv("AMEM_STORE_URL", "http://qdrant:6333")
    monkeypatch.setenv("AMEM_VECTOR_SIZE", "1536")
    cfg = AmemConfig.from_env()
    assert cfg.enabled is False
    assert cfg.store_url == "http://qdrant:6333"
    assert cfg.vector_size == 1536
