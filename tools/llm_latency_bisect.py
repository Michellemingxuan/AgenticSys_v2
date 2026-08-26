"""Where does the latency come from — the transport, or our wrapper around it?

RUN IN THE PRIVATE ENV / ON THE SERVER. Read-only, benign prompts, no case data.

    python tools/llm_latency_bisect.py            # $MODEL, else gpt-4.1
    python tools/llm_latency_bisect.py -n 3       # 3 reps per layer

Context: `tools/safechain_probe.py` measured ~1s per call on the server, but
the app's own prewarm — one "ping", no tools, no response_format, and with NO
node-trace writes (there is no active node at boot, so `create()` early-returns
past the tracing branch) — took 43s on the same host.

That is a ~40x gap with the same model, the same process, and the same network.
This bisects it by timing four layers in ONE process, cheapest first:

  L1  raw safechain          amodel() + chain.ainvoke()      <- what the probe does
  L2  + our bind/message prep _to_lc_messages + _bind_kwargs
  L3  + the firewall gate     async with firewall.gate()
  L4  the real thing          firewalled_client.chat.completions.create()  <- prewarm

The layer where the time appears is the layer to fix. If L1 is already slow
here, the difference is not our code at all but something about how the app's
process is set up (imports, threads, event-loop patching) — run this with
`--import-server` to test that directly.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

MESSAGES = [{"role": "user", "content": "ping"}]


def show(label: str, times: list[float], note: str = "") -> None:
    if not times:
        print(f"  {label:<34} —  {note}")
        return
    med = statistics.median(times)
    print(f"  {label:<34} median {med:8.0f} ms   "
          f"(min {min(times):.0f} / max {max(times):.0f})  {note}")


async def main(model_id: str, reps: int, import_server: bool) -> None:
    if import_server:
        # Reproduce the app's process shape: all of server.py's imports, data
        # loading, background threads and event-loop patching, without serving.
        print("importing server.py (this also runs its prewarm) …")
        t0 = time.perf_counter()
        import server  # noqa: F401
        print(f"  server import took {(time.perf_counter() - t0) * 1000:.0f} ms\n")

    # Importing the client applies nest_asyncio, exactly as the app does.
    from llm.safechain_client import _bind_kwargs, _to_lc_messages
    from llm.firewall_stack import FirewallStack
    from llm.factory import build_session_clients
    from logger.event_logger import EventLogger

    from safechain.core.model import amodel
    from safechain.prompts import ValidChatPromptTemplate
    from langchain_core.prompts import MessagesPlaceholder

    print(f"model={model_id}  reps={reps}\n")

    # ── L1: raw safechain, what the probe does ──────────────────────────
    t0 = time.perf_counter()
    llm = await amodel(model_id)
    build_ms = (time.perf_counter() - t0) * 1000
    print(f"  {'amodel() build':<34} {build_ms:8.0f} ms\n")

    l1 = []
    for _ in range(reps):
        t = time.perf_counter()
        await (ValidChatPromptTemplate.from_messages([MessagesPlaceholder("messages")])
               | llm).ainvoke({"messages": _to_lc_messages(MESSAGES)})
        l1.append((time.perf_counter() - t) * 1000)
    show("L1 raw safechain", l1)

    # ── L2: + our message prep and bind kwargs ──────────────────────────
    l2 = []
    for _ in range(reps):
        t = time.perf_counter()
        bk = _bind_kwargs(None, None, None, extra={})
        bound = llm.bind(**bk) if bk else llm
        await (ValidChatPromptTemplate.from_messages([MessagesPlaceholder("messages")])
               | bound).ainvoke({"messages": _to_lc_messages(MESSAGES)})
        l2.append((time.perf_counter() - t) * 1000)
    show("L2 + bind/message prep", l2)

    # ── L3 / L4: through our client, with the gate ──────────────────────
    firewall = FirewallStack(logger=EventLogger(session_id="bisect"))
    clients = build_session_clients(firewall, model_name=model_id,
                                    backend="safechain")

    l3 = []
    for _ in range(reps):
        t = time.perf_counter()
        async with firewall.gate():
            pass
        l3.append((time.perf_counter() - t) * 1000)
    show("L3 gate acquire only", l3, "(should be ~0)")

    l4 = []
    for _ in range(reps):
        t = time.perf_counter()
        await clients.firewalled_client.chat.completions.create(
            model=model_id, messages=MESSAGES)
        l4.append((time.perf_counter() - t) * 1000)
    show("L4 full client (= prewarm)", l4)

    print("\n  L1 ~= L4  -> our wrapper is fine; look at the process/env")
    print("  L4 >> L1  -> the cost is in the wrapper between them")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?",
                    default=os.environ.get("MODEL", "gpt-4.1"))
    ap.add_argument("-n", "--reps", type=int, default=3)
    ap.add_argument("--import-server", action="store_true",
                    help="import server.py first, to reproduce the app's "
                         "process shape (threads, data load, loop patching)")
    a = ap.parse_args()
    asyncio.run(main(a.model, a.reps, a.import_server))
