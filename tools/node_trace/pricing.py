"""Per-model $/1M-token price table used by NodeTrace cost_usd.

Numbers reflect OpenAI's published list at plan-write time (2026-05-21).
Updates: edit ``_PRICES``. Unknown models cost 0 (cost field stays at 0
so downstream sums aren't poisoned by guesses).
"""
from __future__ import annotations

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
