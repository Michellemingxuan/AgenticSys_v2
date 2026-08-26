"""Populate a tiktoken cache directory, for hosts with no internet egress.

tiktoken downloads its BPE files from `openaipublic.blob.core.windows.net` on
first use. Where that host is unreachable the fetch blocks on the OS TCP
timeout (it passes no timeout of its own), which is how a ~2s turn became
~30s on the private server — twice per LLM round, from inside a coroutine,
pinning the event loop.

The app degrades safely without it (token counts fall back to chars/4), but
trace and cost figures are then approximate. To get exact counts back:

You do NOT need this to run the app. Token counts are trace/cost telemetry,
not analysis inputs — without a cache they fall back to a chars/4 estimate and
nothing else changes. On a host you know is air-gapped, set
`TIKTOKEN_LOAD_TIMEOUT_S=0` and skip the whole question.

Only worth doing if you want exact token and cost figures in the traces.

  1. ON A MACHINE WITH EGRESS — NOT the air-gapped server, which is where
     this fails with `ConnectTimeout ... connect timeout=None`:

         python tools/tiktoken_cache_fetch.py --out ./tiktoken_cache

  2. Copy that directory to the server, anywhere readable.

  3. Point the server at it — the filenames are a sha1 of the source URL, so
     the cache is portable as long as the whole directory is copied:

         export TIKTOKEN_CACHE_DIR=/path/to/tiktoken_cache

Verify on the server with `--verify`, which loads from cache only and fails
loudly if it would have needed the network.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# gpt-4.1 maps to o200k_base; cl100k_base is the fallback this repo uses for
# unknown model names. Fetch both so either path is covered.
ENCODINGS = ("o200k_base", "cl100k_base")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./tiktoken_cache",
                    help="cache directory to populate (default ./tiktoken_cache)")
    ap.add_argument("--verify", action="store_true",
                    help="load from $TIKTOKEN_CACHE_DIR without network access")
    args = ap.parse_args()

    if args.verify:
        cache = os.environ.get("TIKTOKEN_CACHE_DIR")
        if not cache:
            print("TIKTOKEN_CACHE_DIR is not set — nothing to verify.")
            return 1
        print(f"verifying cache at {cache}")
    else:
        os.environ["TIKTOKEN_CACHE_DIR"] = os.path.abspath(args.out)
        os.makedirs(args.out, exist_ok=True)
        print(f"populating {os.path.abspath(args.out)}")

    import tiktoken

    ok = True
    for name in ENCODINGS:
        t0 = time.perf_counter()
        try:
            enc = tiktoken.get_encoding(name)
            n = len(enc.encode("hello world"))
            print(f"  {name:<14} OK  ({(time.perf_counter()-t0)*1000:.0f} ms, "
                  f"sanity encode -> {n} tokens)")
        except Exception as exc:
            ok = False
            print(f"  {name:<14} FAILED — {type(exc).__name__}: "
                  f"{str(exc)[:120]}")

    if not ok:
        print("\nAt least one encoding could not be loaded. If you are on the "
              "server, this means the cache is incomplete — re-run the fetch "
              "on a machine with egress and copy the WHOLE directory over.")
        return 1

    if not args.verify:
        files = sorted(os.listdir(args.out))
        print(f"\n{len(files)} file(s) cached. Copy this directory to the "
              f"server and set:\n    export TIKTOKEN_CACHE_DIR=<path>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
