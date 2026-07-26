from __future__ import annotations

import os
from dataclasses import dataclass

_FALSEY = {"0", "false", "no", "off", ""}


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in _FALSEY


@dataclass(frozen=True)
class AmemConfig:
    enabled: bool
    store_url: str
    collection_name: str
    vector_size: int
    read_timeout_s: float
    write_timeout_s: float
    retrieve_limit: int
    org_id: str
    user_id: str

    @classmethod
    def from_env(cls) -> "AmemConfig":
        return cls(
            enabled=_flag("AMEM_ENABLED", "1"),
            store_url=os.environ.get("AMEM_STORE_URL", "http://127.0.0.1:6333"),
            collection_name=os.environ.get("AMEM_COLLECTION_NAME", "amem_memories"),
            vector_size=int(os.environ.get("AMEM_VECTOR_SIZE", "3072")),
            read_timeout_s=float(os.environ.get("AMEM_READ_TIMEOUT_S", "1.5")),
            write_timeout_s=float(os.environ.get("AMEM_WRITE_TIMEOUT_S", "5.0")),
            retrieve_limit=int(os.environ.get("AMEM_RETRIEVE_LIMIT", "6")),
            org_id=os.environ.get("AMEM_ORG_ID", "amx"),
            user_id=os.environ.get("AMEM_USER_ID", "amx_reviewer"),
        )
