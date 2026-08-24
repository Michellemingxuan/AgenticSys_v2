"""Token estimation must never stall the caller more than once.

Anchored to a real incident. On a server with no egress to
`openaipublic.blob.core.windows.net`, tiktoken's first-use download blocked
until the socket timed out. `_estimate_tokens` caught the failure and fell
back to chars/4 — but did not REMEMBER it, so every call paid the timeout
again. It ran twice per LLM round (prompt + completion), from inside a
coroutine, so the event loop was pinned rather than awaiting: a ~2s turn
measured ~30s, and cancelling one took ~20s to land, which looked like
"safechain isn't cancellable" and wasn't.
"""
import importlib

import pytest

import tools.node_trace.pricing as pricing


@pytest.fixture(autouse=True)
def _fresh():
    """Each test starts with empty caches."""
    importlib.reload(pricing)
    yield


def test_falls_back_to_chars_over_four_when_tiktoken_is_unavailable(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "tiktoken", None)
    # `import tiktoken` yielding None makes attribute access raise -> caught.
    assert pricing.estimate_tokens("x" * 400, "gpt-4.1") == 100


def test_a_failed_lookup_is_tried_once_not_once_per_call(monkeypatch):
    """THE regression. Without negative caching every call re-paid the
    network timeout."""
    calls = {"n": 0}

    class _Boom:
        def encoding_for_model(self, *_a, **_k):
            calls["n"] += 1
            raise RuntimeError("no route to host")

        def get_encoding(self, *_a, **_k):
            calls["n"] += 1
            raise RuntimeError("no route to host")

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Boom())

    for _ in range(10):
        pricing.estimate_tokens("some text to count", "gpt-4.1")

    assert calls["n"] == 1, "tiktoken was consulted more than once after failing"


def test_a_successful_encoder_is_resolved_once_and_reused(monkeypatch):
    resolved = {"n": 0}

    class _Enc:
        def encode(self, text):
            return list(range(len(text.split())))

    class _Tok:
        def encoding_for_model(self, *_a, **_k):
            resolved["n"] += 1
            return _Enc()

        def get_encoding(self, *_a, **_k):
            resolved["n"] += 1
            return _Enc()

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Tok())

    counts = [pricing.estimate_tokens("one two three", "gpt-4.1") for _ in range(5)]

    assert counts == [3] * 5
    assert resolved["n"] == 1


def test_unknown_model_falls_back_to_the_generic_encoding(monkeypatch):
    """Newer model ids are routinely absent from tiktoken's registry; that is
    not a failure and must not poison the cache."""
    used = {}

    class _Enc:
        def encode(self, text):
            return list(range(len(text)))

    class _Tok:
        def encoding_for_model(self, name):
            raise KeyError(name)

        def get_encoding(self, name):
            used["name"] = name
            return _Enc()

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Tok())

    assert pricing.estimate_tokens("abcd", "some-model-from-2027") == 4
    assert used["name"] == "cl100k_base"


def test_empty_text_costs_nothing_and_touches_no_encoder(monkeypatch):
    class _Boom:
        def encoding_for_model(self, *_a, **_k):
            raise AssertionError("should not be consulted for empty text")
        get_encoding = encoding_for_model

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Boom())
    assert pricing.estimate_tokens("", "gpt-4.1") == 0


def test_prewarm_reports_availability(monkeypatch):
    class _Boom:
        def encoding_for_model(self, *_a, **_k):
            raise RuntimeError("offline")
        get_encoding = encoding_for_model

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Boom())
    assert pricing.prewarm_encoder("gpt-4.1") is False
    # And having reported it, it does not keep retrying.
    assert pricing.estimate_tokens("x" * 40, "gpt-4.1") == 10


def test_both_call_sites_use_the_shared_implementation():
    """The duplication is what hid this: two copies, each calling tiktoken
    directly, so fixing one would have left the other stalling."""
    from pathlib import Path
    for mod in ("llm/safechain_client.py", "tools/node_trace/hooks.py"):
        src = Path(mod).read_text()
        assert "import tiktoken" not in src, f"{mod} still calls tiktoken directly"
        assert "from tools.node_trace.pricing import estimate_tokens" in src


def test_an_unbounded_download_cannot_stall_the_caller(monkeypatch):
    """tiktoken calls `requests.get` with NO timeout, so an unreachable host
    waits out the OS TCP connect timeout (~2 min). Observed on the server:

        ConnectTimeoutError(... 'connect timeout=None') -> MaxRetryError

    The caller must be bounded even though the fetch is not.
    """
    import time

    class _Hang:
        def encoding_for_model(self, *_a, **_k):
            time.sleep(30)          # stands in for the TCP timeout
            raise RuntimeError("unreachable")
        get_encoding = encoding_for_model

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Hang())
    monkeypatch.setattr(pricing, "_LOAD_TIMEOUT_S", 0.2)

    t0 = time.perf_counter()
    n = pricing.estimate_tokens("x" * 400, "gpt-4.1")
    elapsed = time.perf_counter() - t0

    assert n == 100                       # fell back to chars/4
    assert elapsed < 2.0, f"caller blocked for {elapsed:.1f}s"

    # And having given up once, later calls do not wait at all.
    t0 = time.perf_counter()
    pricing.estimate_tokens("x" * 400, "gpt-4.1")
    assert (time.perf_counter() - t0) < 0.05


def test_a_late_arriving_encoder_is_still_used(monkeypatch):
    """A slow fetch that beats the deadline by a hair should not be thrown
    away — the cache is consulted before the unavailable set for this reason."""
    import time

    class _Enc:
        def encode(self, text):
            return list(range(len(text.split())))

    class _Slow:
        def encoding_for_model(self, *_a, **_k):
            time.sleep(0.3)
            return _Enc()
        get_encoding = encoding_for_model

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Slow())
    monkeypatch.setattr(pricing, "_LOAD_TIMEOUT_S", 0.05)

    assert pricing.estimate_tokens("one two three", "gpt-4.1") == 3  # gave up
    time.sleep(0.5)                                                  # it lands
    assert pricing.estimate_tokens("one two three", "gpt-4.1") == 3  # now exact
    assert "gpt-4.1" in pricing._ENCODERS


def test_timeout_zero_skips_the_attempt_entirely(monkeypatch):
    """On a host known to be air-gapped, paying a probe (and leaving a thread
    stuck in an unconnectable socket) on every boot is pure waste."""
    touched = {"n": 0}

    class _Boom:
        def encoding_for_model(self, *_a, **_k):
            touched["n"] += 1
            raise AssertionError("tiktoken should not be consulted at all")
        get_encoding = encoding_for_model

    monkeypatch.setitem(__import__("sys").modules, "tiktoken", _Boom())
    monkeypatch.setattr(pricing, "_LOAD_TIMEOUT_S", 0.0)

    assert pricing.estimate_tokens("x" * 400, "gpt-4.1") == 100
    assert pricing.prewarm_encoder("gpt-4.1") is False
    assert touched["n"] == 0
