"""Per-model $/1M-token price table used by NodeTrace cost_usd.

Numbers reflect OpenAI's published list at plan-write time (2026-05-21).
Updates: edit ``_PRICES``. Unknown models cost 0 (cost field stays at 0
so downstream sums aren't poisoned by guesses).
"""
from __future__ import annotations

import os
import threading

# Per-million-token rates (input, cached_input, output).
# cached_input defaults to half the input rate when not explicitly listed.
_PRICES: dict[str, tuple[float, float | None, float]] = {
    "gpt-4o":        (2.50, 1.25, 10.00),
    "gpt-4o-mini":   (0.15, 0.075, 0.60),
    "gpt-4-turbo":   (10.00, None, 30.00),
    "gpt-4":         (30.00, None, 60.00),
    "gpt-3.5-turbo": (0.50, None, 1.50),
    "o1":            (15.00, 7.50, 60.00),
    "o1-mini":       (3.00, 1.50, 12.00),
    "o3-mini":       (1.10, 0.55, 4.40),
}


def _normalize_model(model: str) -> str:
    """Strip date / version suffixes (OpenAI returns ``gpt-4o-2024-08-06``)."""
    m = model.lower()
    # Try longest-prefix match so "gpt-4o-mini-…" doesn't collapse to "gpt-4o".
    for prefix in sorted(_PRICES, key=len, reverse=True):
        if m.startswith(prefix):
            return prefix
    return m


def compute_cost(
    *,
    model: str | None,
    prompt_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> float:
    if not model:
        return 0.0
    key = _normalize_model(model)
    if key not in _PRICES:
        return 0.0
    rate_in, rate_cached, rate_out = _PRICES[key]
    if rate_cached is None:
        rate_cached = rate_in / 2
    p_in = prompt_tokens or 0
    p_cached = min(cached_input_tokens or 0, p_in)
    p_fresh = p_in - p_cached
    p_out = completion_tokens or 0
    return (
        p_fresh * rate_in / 1_000_000
        + p_cached * rate_cached / 1_000_000
        + p_out * rate_out / 1_000_000
    )


# ── Token estimation ────────────────────────────────────────────────────────
# ONE implementation, because there were two and they hid a 30s-per-turn stall
# between them (`llm/safechain_client.py` and `tools/node_trace/hooks.py` each
# had their own copy, each calling tiktoken directly).
#
# tiktoken fetches its BPE file over the network on first use
# (openaipublic.blob.core.windows.net). In an environment with no egress to
# that host the call BLOCKS until the socket times out — and these run inside
# coroutines, so the event loop is pinned rather than awaiting. Nothing else
# on the loop progresses: not the next task, not a `wait_for` timeout, not the
# cancellation it was asked to deliver.
#
# Two things made that far worse than a one-off stall:
#   • Two calls per LLM round (prompt and completion), so double the timeout.
#   • The old `except Exception: return len(text) // 4` fallback caught the
#     failure but did not REMEMBER it, so every subsequent call paid the
#     timeout again. A ~2s turn measured ~30s; cancelling one took ~20s to
#     land, which read as "safechain isn't cancellable" and was not.
#
# So the encoder is resolved at most once per model, and a failure is cached
# too: after the first miss every later call takes the chars/4 path instantly.
_ENCODERS: dict[str, object] = {}
_ENCODER_UNAVAILABLE: set[str] = set()
_ENCODER_LOCK = threading.Lock()

# How long to wait for tiktoken to produce an encoder before giving up on it.
#
# Bounded because tiktoken's fetch is UNBOUNDED: `tiktoken/load.py` calls
# `requests.get(blobpath)` with no timeout, so a host that cannot reach
# openaipublic.blob.core.windows.net waits out the OS TCP connect timeout
# (~2 min on Linux) and urllib3 retries on top of that. Observed on a server
# with no egress:
#     ConnectTimeoutError(... 'connect timeout=None') -> MaxRetryError
# Five seconds is far longer than a cache hit (milliseconds) and far shorter
# than the failure, which is exactly the gap we need.
#
# Set to 0 on a host you KNOW is air-gapped: it skips the attempt entirely, so
# there is no 5s boot probe and no background thread left sitting in a socket
# that cannot connect. Counts then come from the chars/4 estimate, which is
# what they were going to be anyway on such a host — the only thing lost is
# precision in trace/cost telemetry, never correctness of an answer.
_LOAD_TIMEOUT_S = float(os.environ.get("TIKTOKEN_LOAD_TIMEOUT_S", "5"))


def _load_encoder(key: str):
    """Import tiktoken and build the encoder. May block on the network."""
    import tiktoken
    try:
        return tiktoken.encoding_for_model(key)
    except KeyError:
        # Unknown model name — the generic encoding is close enough for an
        # estimate, and this is the common path for newer model ids.
        return tiktoken.get_encoding("cl100k_base")


def _resolve_encoder(model: str | None, timeout_s: float | None = None):
    """The tiktoken encoder for `model`, or None. Never retried after a miss.

    The lookup runs on a worker thread with a deadline, so no caller can be
    blocked for longer than `_LOAD_TIMEOUT_S` — the download itself has no
    timeout at all. If a slow fetch later succeeds it populates `_ENCODERS`
    anyway, and subsequent calls pick it up for free: the cache is checked
    before the unavailable set precisely so a late arrival still wins.
    """
    key = model or "cl100k_base"
    if key in _ENCODERS:
        return _ENCODERS[key]
    if key in _ENCODER_UNAVAILABLE:
        return None

    budget = _LOAD_TIMEOUT_S if timeout_s is None else timeout_s
    if budget <= 0:
        # Explicitly opted out. Don't spawn a thread that would sit in a
        # socket for the OS TCP timeout on a host with no route out.
        _ENCODER_UNAVAILABLE.add(key)
        return None

    with _ENCODER_LOCK:
        # Re-check: another thread may have resolved it while we waited.
        if key in _ENCODERS:
            return _ENCODERS[key]
        if key in _ENCODER_UNAVAILABLE:
            return None

        box: dict[str, object] = {}

        def _work() -> None:
            try:
                enc = _load_encoder(key)
            except Exception:
                box["err"] = True
                return
            # Publish from the WORKER, not from the caller. If we blew the
            # deadline the caller has already given up and moved on, and an
            # encoder that lands a moment later is still worth having — a
            # dict assignment is atomic under the GIL, so no lock is needed.
            _ENCODERS[key] = enc
            box["enc"] = enc

        t = threading.Thread(target=_work, name=f"tiktoken-load-{key}",
                             daemon=True)
        t.start()
        t.join(budget)

        if "enc" in box:
            _ENCODERS[key] = box["enc"]
            return box["enc"]
        # Timed out or failed. Either way stop asking — a still-running thread
        # is harmless and will fill `_ENCODERS` if it ever finishes.
        _ENCODER_UNAVAILABLE.add(key)
        return None


def estimate_tokens(text: str, model: str | None = None) -> int:
    """Approximate token count. Falls back to chars/4 when tiktoken can't load.

    Approximate by design — safechain returns no usage object, so this is the
    only token signal on that path, and rows built from it are flagged
    `tokens_estimated` so the number is never mistaken for exact.
    """
    if not text:
        return 0
    enc = _resolve_encoder(model)
    if enc is None:
        return max(1, len(text) // 4)
    try:
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def prewarm_encoder(model: str | None = None,
                    timeout_s: float | None = None) -> bool:
    """Resolve the encoder ahead of the hot path. True if one is available.

    Called at boot so an environment without egress settles the question once,
    during startup, instead of inside the reviewer's first question.
    """
    return _resolve_encoder(model, timeout_s) is not None
