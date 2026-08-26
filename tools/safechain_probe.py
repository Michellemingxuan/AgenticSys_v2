"""Probe the private-env safechain build for native structured output + tool calling.

Answers the six unknowns blocking the `llm/safechain_client.py` rework: today
every tool schema and response schema is injected as TEXT and the reply is
recovered by ~500 lines of JSON repair, which is where ModelBehaviorError comes
from. If this build supports `bind_tools` / `with_structured_output` natively,
that layer can go.

RUN IN THE PRIVATE ENV ONLY — safechain is not installed in dev.

    python tools/safechain_probe.py                     # $SAFECHAIN_MODEL, else gpt-4o
    python tools/safechain_probe.py gpt-4o gpt-5        # specific models
    python tools/safechain_probe.py --reject-probe "<text your firewall blocks>"

Bootstraps the same way the app does (see the block below): repo root on
sys.path, `load_dotenv` for credentials, and `nest_asyncio.apply()` — that last
one is REQUIRED here, because safechain's token acquisition calls
`asyncio.run(...)` internally and this probe runs inside an event loop.
Needs `python-dotenv` and `nest_asyncio`, both already in requirements.txt.

Read-only: it builds models and makes a handful of tiny calls (benign prompts,
no case data, no PII). Roughly 5 short calls per model. Every probe is fenced,
so a failure is REPORTED rather than fatal — a red line here is a real answer,
not a broken script.
"""
from __future__ import annotations

import argparse
import asyncio
import time
import inspect
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# ── bootstrap: mirror how the app itself starts up ──────────────────────────
# Order matters and each step is load-bearing in the private env.

# 1. Repo root on sys.path — `python tools/safechain_probe.py` puts `tools/` on
#    the path, not the project root, so project imports would fail.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 2. Console encoding. This file prints box-drawing characters and em-dashes;
#    on a cp1252 Windows console that is a UnicodeEncodeError mid-report.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except (AttributeError, ValueError):
    pass

# 3. Credentials. `server.py` does exactly this before touching safechain —
#    `amodel()` acquires a token from the environment, so without it the build
#    fails with an auth error that looks like a safechain bug.
try:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(), override=True)
except ImportError:
    print("!! python-dotenv missing — env credentials may not be loaded")

# 4. nest_asyncio. THE critical one for this script: safechain's underlying
#    `TokenUtil.get_token` bridges sync->async with `asyncio.run(...)`, which
#    raises "asyncio.run() cannot be called from a running event loop" the
#    moment it is called from inside one — and this whole probe runs under
#    `asyncio.run(main())`. `llm/safechain_client.py` applies this patch at
#    module import for the same reason; the probe must too.
_NEST_ASYNCIO_APPLIED = False
try:
    import nest_asyncio  # type: ignore[import-not-found]

    nest_asyncio.apply()
    _NEST_ASYNCIO_APPLIED = True
except ImportError:
    print("!! nest_asyncio missing — `pip install nest_asyncio`. Without it, "
          "amodel() will likely fail with 'asyncio.run() cannot be called from "
          "a running event loop'.")

import warnings

# Benign: langchain_openai's structured-output path round-trips the parsed
# model through a field typed `None`, and pydantic warns. Not a data problem —
# filtered so it can't be mistaken for one in the report.
warnings.filterwarnings(
    "ignore", message=".*PydanticSerializationUnexpectedValue.*")
warnings.filterwarnings("ignore", message=".*serialized value may not be.*")

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - pydantic ships with langchain
    print("pydantic missing — run inside the project venv")
    raise SystemExit(2)

# 5. langchain pieces the chains are built from. Fenced and hoisted: an
#    unfenced import down inside a probe aborts that model's entire run, which
#    would hide every other answer behind one missing package.
try:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import PromptTemplate

    _LANGCHAIN_OK = True
except ImportError as _e:
    StrOutputParser = PromptTemplate = None  # type: ignore[assignment]
    _LANGCHAIN_OK = False
    print(f"!! langchain_core missing ({_e}) — chain probes will be skipped")


# Deliberately trivial and non-sensitive: we are testing the TRANSPORT, not the
# model's reasoning. Two fields of different types is enough to prove the schema
# round-tripped (a text parser that guesses will usually get `count` wrong).
class ProbeShape(BaseModel):
    """Structured-output probe payload."""

    color: str = Field(description="The color named in the request.")
    count: int = Field(description="The integer named in the request.")


# Bump on every change to the checks. Printed first, and echoed in the summary,
# so a run from a STALE COPY is obvious — this script is edited here but run on
# a different machine, so "the check didn't appear" is far more often an
# out-of-date file than a real skip.
PROBE_REVISION = "r5 (adds per-call latency)"

PROBE_PROMPT = "Return color='blue' and count=7. Use exactly those values."

# OpenAI-shaped tool definition, STRICT — matching what `@function_tool` emits
# in production (`strict_json_schema=True`). Strict mode requires
# `additionalProperties: false` and every property listed in `required`.
STRICT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

# Resolved the same way the app does: `safechain_client._arefresh_llm` reads
# `SAFECHAIN_MODEL`, falling back to the `model_name` passed by
# `llm/factory.py` (default "gpt-4o"). Probing whatever prod actually runs
# beats probing a model id the deployment never uses.
DEFAULT_MODELS = [os.environ.get("SAFECHAIN_MODEL", "gpt-4o")]

# Bounded so a hung build can't stall the whole probe. Overridable: a TIMEOUT
# verdict means "we don't know", which is worth paying more to resolve.
CALL_TIMEOUT_S = 90.0

# Substring filter over check names (`--only`), so a follow-up run can re-probe
# just the unresolved checks instead of re-paying for the settled ones.
ONLY: list[str] = []


# Prerequisites, not checks: filtering these out would leave every later probe
# with no model to probe against.
_ALWAYS_RUN = frozenset({"build"})


def selected(check: str) -> bool:
    if check in _ALWAYS_RUN:
        return True
    return not ONLY or any(o.lower() in check.lower() for o in ONLY)


# ── reporting ───────────────────────────────────────────────────────────────

RESULTS: list[tuple[str, str, str]] = []   # (model, check, verdict)


def record(model: str, check: str, verdict: str) -> None:
    RESULTS.append((model, check, verdict))


def hdr(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def line(label: str, value: Any) -> None:
    print(f"  {label:.<42} {value}")


def exc_detail(e: BaseException) -> str:
    """Exception type + message + args — question 5 hinges on the exact shape."""
    return (f"{type(e).__module__}.{type(e).__name__}: {str(e)[:300]}"
            f" | args={[str(a)[:120] for a in getattr(e, 'args', [])][:3]}")


# Wall-clock per live call, in call order. The probe answers "does this build
# SUPPORT X"; these answer "how long does X take here", which is what you need
# when the same build is fast on one host and slow on another. Without them a
# comparison between two environments can only say "both work".
CALL_TIMINGS: list[tuple[str, str, float]] = []


async def attempt(model: str, check: str, coro_factory) -> tuple[bool, Any]:
    """Run one probe; never raise. Returns (ok, result_or_exception).

    Tolerates a SYNC callable: we're probing an API whose async-ness is one of
    the things under test, so a non-awaitable return is a finding to report,
    not a TypeError that aborts the probe.

    Records elapsed wall-clock for every attempt, including failures — a call
    that fails SLOWLY (an internal retry ladder exhausting itself) looks
    nothing like one that fails fast, and the difference is the whole point.
    """
    if not selected(check):
        return False, None
    t0 = time.perf_counter()
    try:
        produced = coro_factory()
        if not inspect.isawaitable(produced):
            record(model, check, f"SYNC (not awaitable) -> {type(produced).__name__}")
            return True, produced
        result = await asyncio.wait_for(produced, timeout=CALL_TIMEOUT_S)
        return True, result
    except asyncio.TimeoutError:
        record(model, check, f"TIMEOUT (>{CALL_TIMEOUT_S:.0f}s)")
        return False, None
    except Exception as e:  # noqa: BLE001 - reporting a failure IS the output
        record(model, check, f"FAILED — {exc_detail(e)}")
        return False, e
    finally:
        CALL_TIMINGS.append(
            (model, check, (time.perf_counter() - t0) * 1000.0))


def print_timings(hdr) -> None:
    """Per-call latency, plus the stats you actually compare across hosts.

    Printed even when every check passed: "all OK at 2s/call" and "all OK at
    40s/call" are the same verdict table and completely different findings.
    """
    if not CALL_TIMINGS:
        return
    hdr("LATENCY — compare these across environments")
    for model, check, ms in CALL_TIMINGS:
        line(f"{model} · {check}"[:54], f"{ms:8.0f} ms")
    times = sorted(ms for _, _, ms in CALL_TIMINGS)
    print()
    line("live calls", len(times))
    line("median", f"{times[len(times) // 2]:8.0f} ms")
    line("min / max", f"{times[0]:.0f} ms / {times[-1]:.0f} ms")
    line("total", f"{sum(times) / 1000.0:8.1f} s")


# ── Q0: what is importable ──────────────────────────────────────────────────

def probe_imports() -> dict:
    hdr("Q0 — environment + safechain surface")

    line("nest_asyncio applied", "yes" if _NEST_ASYNCIO_APPLIED else "NO (see warning above)")
    line("SAFECHAIN_MODEL env", os.environ.get("SAFECHAIN_MODEL", "(unset -> gpt-4o)"))
    line("SAFECHAIN_CALL_TIMEOUT_S", os.environ.get("SAFECHAIN_CALL_TIMEOUT_S", "(unset)"))

    try:
        import safechain  # type: ignore[import-not-found]

        line("safechain.__version__", getattr(safechain, "__version__", "(unset)"))
        line("safechain.__file__", getattr(safechain, "__file__", "?"))
    except Exception as e:  # noqa: BLE001
        line("safechain", f"NOT IMPORTABLE — {e}")
        print("\n  >>> Run this in the PRIVATE env. Nothing else will work here.")
        # Returning None rather than raising: under nest_asyncio a SystemExit
        # from inside the coroutine surfaces as a noisy "Task exception was
        # never retrieved" traceback that buries the one line that matters.
        return None

    # Submodule listing via pkgutil, which reads the package directory WITHOUT
    # importing — so a submodule that fails on import can't take the probe with
    # it, and we still learn it exists.
    print("\n  -- safechain submodules (not imported) --")
    try:
        import pkgutil

        names = sorted(m.name for m in pkgutil.iter_modules(safechain.__path__))
        line("submodules", ", ".join(names) or "(none found)")
        for sub in ("core", "prompts", "lcel"):
            if sub in names:
                try:
                    subpkg = __import__(f"safechain.{sub}", fromlist=["*"])
                    if hasattr(subpkg, "__path__"):
                        line(f"safechain.{sub}.*", ", ".join(sorted(
                            m.name for m in pkgutil.iter_modules(subpkg.__path__))))
                except Exception as e:  # noqa: BLE001
                    line(f"safechain.{sub}.*", f"-- {type(e).__name__}: {str(e)[:60]}")
    except Exception as e:  # noqa: BLE001
        line("submodules", f"-- {type(e).__name__}: {e}")

    # DISCOVER rather than guess. The v1 note pointed at `safechain.lcel.model`,
    # which this build doesn't have — hardcoding names just moves the guess.
    # Enumerate each candidate module's public surface, and pick the model
    # factory out of it by shape.
    print("\n  -- module contents --")
    found: dict = {}
    model_factories: dict[str, Any] = {}
    for path in ("safechain.core.model", "safechain.prompts", "safechain.lcel",
                 "safechain.core", "safechain.model"):
        try:
            mod = __import__(path, fromlist=["*"])
        except Exception as e:  # noqa: BLE001
            line(path, f"-- {type(e).__name__}: {str(e)[:70]}")
            continue
        public = [n for n in dir(mod) if not n.startswith("_")]
        line(path, ", ".join(public[:24]) + (" …" if len(public) > 24 else ""))
        for name in public:
            obj = getattr(mod, name, None)
            low = name.lower()
            # "chat" catches SafeAzureChatOpenAI-style factories, but also
            # ValidChatPromptTemplate — which is a PROMPT, not a model, and
            # would be a disastrous pick for the amodel fallback below.
            if any(w in low for w in ("prompt", "template", "parser")):
                continue
            if callable(obj) and any(w in low for w in ("model", "chat", "llm")):
                model_factories.setdefault(f"{path}.{name}", obj)

    line("model-ish callables", ", ".join(sorted(model_factories)) or "(none)")
    record("env", "model-ish callables", ", ".join(sorted(model_factories)) or "(none)")

    # The two the client actually depends on (safechain_client.py:189 and :344).
    # A miss here is a real finding, not a script bug — so fall back to whatever
    # discovery turned up so the rest of the probe can still run.
    for label, path, attr in (
        ("amodel", "safechain.core.model", "amodel"),
        ("ValidChatPromptTemplate", "safechain.prompts", "ValidChatPromptTemplate"),
    ):
        try:
            mod = __import__(path, fromlist=[attr])
            found[label] = getattr(mod, attr)
            line(f"{label} [used by client]", f"OK  ({path}.{attr})")
        except Exception as e:  # noqa: BLE001
            line(f"{label} [used by client]", f"MISSING — {type(e).__name__}: {str(e)[:70]}")
            record("env", f"{path}.{attr}", f"MISSING — {type(e).__name__}")

    if "amodel" not in found:
        # Prefer an async factory; fall back to any model-ish callable.
        pick = next((k for k in sorted(model_factories)
                     if k.rsplit(".", 1)[-1] in ("amodel", "aget_model", "async_model")), None)
        pick = pick or next(iter(sorted(model_factories)), None)
        if pick:
            found["amodel"] = model_factories[pick]
            line("amodel fallback", f"using {pick}")
            record("env", "amodel fallback", pick)

    return found


# ── per-model probes ────────────────────────────────────────────────────────

async def probe_model(model_id: str, surface: dict, reject_probe: str | None) -> None:
    hdr(f"MODEL: {model_id}")

    amodel = surface.get("amodel")
    if amodel is None:
        record(model_id, "build", "FAILED — safechain.core.model.amodel not importable")
        return

    ok, m = await attempt(model_id, "build", lambda: amodel(model_id))
    if not ok:
        return
    line("built type", f"{type(m).__module__}.{type(m).__name__}")
    line("MRO", " <- ".join(c.__name__ for c in type(m).__mro__[:5]))

    # ── Q1/Q2 capability flags (cheap, no calls) ─────────────────────────
    print("\n  -- capability flags (no API calls) --")
    for attr in ("bind_tools", "with_structured_output", "ainvoke",
                 "_agenerate", "_astream", "astream"):
        has = hasattr(m, attr)
        line(attr, "yes" if has else "NO")
        if attr in ("bind_tools", "with_structured_output"):
            record(model_id, f"has {attr}", "yes" if has else "NO")

    # Q2: which `method=` values does with_structured_output accept? A native
    # json_schema / function_calling path is what actually kills format errors;
    # a text parser under the hood would leave us roughly where we are.
    if hasattr(m, "with_structured_output"):
        try:
            sig = inspect.signature(m.with_structured_output)
            line("with_structured_output sig", str(sig)[:160])
            src = ""
            try:
                src = inspect.getsource(type(m).with_structured_output)
            except (OSError, TypeError):
                pass
            methods = [v for v in ("json_schema", "function_calling", "json_mode")
                       if v in src]
            line("method= values seen in source", methods or "(could not read source)")
            record(model_id, "structured methods", ", ".join(methods) or "unknown")
        except Exception as e:  # noqa: BLE001
            line("with_structured_output sig", f"?? {e}")

    # ── Q6 COMPLIANCE (static, no API calls) — where does redaction live? ──
    # The rewrite calls `.bind(...).ainvoke(messages)` directly, which BYPASSES
    # ValidChatPromptTemplate. That is only safe if the policy enforcement that
    # matters sits in the MODEL (InputRedactor is in its MRO) rather than in the
    # template. Static introspection only — this decides whether the rewrite is
    # shippable, so it must not be guessed from behavior.
    print("\n  -- compliance surface (static) --")
    for klass in type(m).__mro__:
        if klass.__name__ in ("InputRedactor", "OpenAIMiddleware"):
            methods = [n for n in vars(klass) if not n.startswith("__")]
            line(f"{klass.__name__} defines", ", ".join(sorted(methods))[:200])
            record(model_id, f"{klass.__name__} methods",
                   ", ".join(sorted(methods))[:160])
            try:
                src = inspect.getsource(klass)
                hooks = [h for h in ("_agenerate", "_generate", "_astream",
                                     "_stream", "invoke", "ainvoke", "redact",
                                     "sanitize", "validate")
                         if f"def {h}" in src]
                line(f"{klass.__name__} overrides", hooks or "(none of interest)")
                record(model_id, f"{klass.__name__} overrides", ", ".join(hooks) or "none")
            except (OSError, TypeError) as e:
                line(f"{klass.__name__} source", f"unreadable: {e}")

    VCPT_cls = surface.get("ValidChatPromptTemplate")
    if VCPT_cls is not None:
        line("VCPT MRO", " <- ".join(c.__name__ for c in VCPT_cls.__mro__[:4]))
        own = [n for n in vars(VCPT_cls) if not n.startswith("__")]
        line("VCPT defines", ", ".join(sorted(own))[:200] or "(nothing of its own)")
        record(model_id, "VCPT defines",
               ", ".join(sorted(own))[:160] or "(nothing of its own — "
               "likely a thin subclass, so bypassing it may be safe)")

    # ── R3 — THE SHIPPING SHAPE: keep the firewall template AND go native ──
    # Compliance (above) splits in two: InputRedactor lives in the MODEL and
    # redacts message-list inputs either way, but VCPT.format_prompt is
    # template-time and WOULD be skipped by a bare `.ainvoke(messages)`.
    #
    # No need to choose. The SDK hands us an arbitrary runtime message list, and
    # MessagesPlaceholder is exactly the ChatPromptTemplate feature for that —
    # so if this composes, the rewrite keeps VCPT in the path AND gets native
    # tools + response_format + streaming. This is the shape to implement.
    _r3_blockers = [
        name for name, ok_ in (
            ("langchain_core importable", _LANGCHAIN_OK),
            ("ValidChatPromptTemplate found", VCPT_cls is not None),
            ("model has .bind", hasattr(m, "bind")),
        ) if not ok_
    ]
    if _r3_blockers:
        # Never skip silently: an absent check reads as "didn't happen" when it
        # actually means "couldn't run", and those need very different fixes.
        line("R3 SKIPPED — unmet", ", ".join(_r3_blockers))
        record(model_id, "R3 VCPT + MessagesPlaceholder + bind(tools)",
               f"SKIPPED — unmet: {', '.join(_r3_blockers)}")
    else:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_core.prompts import MessagesPlaceholder

            async def _shipping_shape():
                chain = (VCPT_cls.from_messages([MessagesPlaceholder("messages")])
                         | m.bind(tools=[STRICT_TOOL], tool_choice="required"))
                return await chain.ainvoke({"messages": [
                    SystemMessage(content="You look up weather."),
                    HumanMessage(content="Weather in Phoenix?"),
                ]})

            ok, res = await attempt(
                model_id, "R3 VCPT + MessagesPlaceholder + bind(tools)",
                _shipping_shape)
            if ok:
                calls = getattr(res, "tool_calls", None)
                line("VCPT + placeholder + native tools",
                     json.dumps(calls, default=str)[:160] if calls else "NO tool_calls")
                record(model_id, "R3 VCPT + MessagesPlaceholder + bind(tools)",
                       f"OK — firewall template KEPT, native tools work: "
                       f"{json.dumps(calls, default=str)[:90]}" if calls
                       else "composed but NO tool_calls")
        except Exception as e:  # noqa: BLE001
            record(model_id, "R3 VCPT + MessagesPlaceholder + bind(tools)",
                   f"FAILED to set up — {exc_detail(e)}")

    # ── Q2 live: structured output WITHOUT the firewall template ─────────
    # This mirrors the snippet that motivated the work.
    print("\n  -- live calls --")
    if _LANGCHAIN_OK and hasattr(m, "with_structured_output"):

        async def _plain_structured():
            chain = PromptTemplate.from_template("{q}") | m.with_structured_output(ProbeShape)
            return await chain.ainvoke({"q": PROBE_PROMPT})

        ok, res = await attempt(model_id, "structured (PromptTemplate)", _plain_structured)
        if ok:
            good = isinstance(res, ProbeShape) and res.color and res.count == 7
            line("structured via PromptTemplate",
                 f"{'OK' if good else 'RETURNED BUT ODD'} -> {res!r}"[:150])
            record(model_id, "structured (PromptTemplate)",
                   f"OK type={type(res).__name__} count_exact={res.count == 7}"
                   if good else f"returned {type(res).__name__}: {res!r}"[:120])

    # ── Q3 live: structured output WITH ValidChatPromptTemplate ──────────
    # If this works, we keep the firewall wrapper AND gain schema adherence.
    VCPT = surface.get("ValidChatPromptTemplate")
    if _LANGCHAIN_OK and VCPT is not None and hasattr(m, "with_structured_output"):
        async def _vcpt_structured():
            chain = (VCPT.from_messages([("human", "{__input__}")])
                     | m.with_structured_output(ProbeShape))
            return await chain.ainvoke({"__input__": PROBE_PROMPT})

        ok, res = await attempt(model_id, "structured (ValidChatPromptTemplate)",
                                _vcpt_structured)
        if ok:
            good = isinstance(res, ProbeShape)
            line("structured via VCPT", f"{'OK' if good else 'ODD'} -> {res!r}"[:150])
            record(model_id, "structured (ValidChatPromptTemplate)",
                   f"OK -> {res!r}"[:120] if good else f"{type(res).__name__}")

    # ── Q4 live: does the template accept role-tagged multi-message input? ──
    if _LANGCHAIN_OK and VCPT is not None:
        async def _multi_message():
            chain = (VCPT.from_messages([("system", "{sys}"), ("human", "{q}")])
                     | m | StrOutputParser())
            return await chain.ainvoke(
                {"sys": "Answer with a single word.", "q": "Say the word blue."})

        ok, res = await attempt(model_id, "multi-message (system+human)", _multi_message)
        if ok:
            line("system+human accepted", f"OK -> {str(res)[:60]!r}")
            record(model_id, "multi-message (system+human)", "OK — accepted")

    # ── Q1 live: native tool calling ─────────────────────────────────────
    # The big one: if this works, the whole text tool-call protocol and its
    # repair layer can be deleted, not just the final-output parsing.
    if hasattr(m, "bind_tools"):
        class GetWeather(BaseModel):
            """Look up the weather for a city."""

            city: str = Field(description="City name.")

        async def _tools():
            bound = m.bind_tools([GetWeather])
            return await bound.ainvoke("What is the weather in Phoenix? Use the tool.")

        ok, res = await attempt(model_id, "bind_tools", _tools)
        if ok:
            calls = getattr(res, "tool_calls", None)
            line("response type", type(res).__name__)
            line("tool_calls", json.dumps(calls, default=str)[:200] if calls else "EMPTY")
            record(model_id, "bind_tools",
                   f"NATIVE — {json.dumps(calls, default=str)[:120]}" if calls
                   else "callable but returned NO tool_calls (text only)")

        # ── ROUND 2 — the questions that decide the REWRITE's shape ──────
        # Round 1 proved the capabilities exist. These decide whether the shim
        # can keep mimicking AsyncOpenAI at the HTTP boundary (just forwarding
        # the SDK's own kwargs) or has to be restructured.

        # R2a — THE decisive one. The openai-agents SDK hands the shim
        # OpenAI-shaped `tools` + `response_format` + `tool_choice` in ONE
        # call (specialists have tools AND output_type=SpecialistOutput). If
        # BaseChatOpenAI forwards these verbatim via .bind(), the rewrite is
        # ~"pass the kwargs through and convert AIMessage -> ChatCompletion",
        # and `_combine_messages` / the whole repair layer just deletes.
        oai_tool = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Look up the weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
        async def _passthrough():
            return await m.bind(tools=[oai_tool]).ainvoke(
                "What is the weather in Phoenix? Use the tool.")

        ok, res = await attempt(model_id, "R2a .bind(openai tools=) passthrough",
                                _passthrough)
        if ok:
            calls = getattr(res, "tool_calls", None)
            line("openai-shaped tools via .bind()",
                 json.dumps(calls, default=str)[:160] if calls else "NO tool_calls")
            record(model_id, "R2a .bind(openai tools=) passthrough",
                   f"OK — {json.dumps(calls, default=str)[:110]}" if calls
                   else "accepted but NO tool_calls")

        # R2b — tools AND a response schema in the same request. The SDK sends
        # both on every specialist round, so this combination is mandatory.
        #
        # The first attempt failed with "`get_weather` is not strict. Only
        # `strict` function tools can be auto-parsed" — an OpenAI auto-parse
        # rule, not a safechain limit. What production actually sends is
        # STRICT tools (`@function_tool` -> strict_json_schema=True) with a
        # NON-strict response schema (`AgentOutputSchema(..., strict_json_schema
        # =False)`), so probe that exact pairing, plus the all-strict variant.
        strict_tool = STRICT_TOOL
        strict_schema = dict(ProbeShape.model_json_schema())
        strict_schema["additionalProperties"] = False

        for label, tool_def, rf in (
            # The pairing production actually uses.
            ("R2b strict tools + NON-strict response_format", strict_tool,
             {"type": "json_schema",
              "json_schema": {"name": "ProbeShape",
                              "schema": ProbeShape.model_json_schema(),
                              "strict": False}}),
            # The all-strict variant, in case auto-parse demands it.
            ("R2b strict tools + strict response_format", strict_tool,
             {"type": "json_schema",
              "json_schema": {"name": "ProbeShape", "schema": strict_schema,
                              "strict": True}}),
        ):
            async def _both(_t=tool_def, _rf=rf):
                return await m.bind(tools=[_t], response_format=_rf).ainvoke(
                    "Return color='blue' and count=7.")

            ok, res = await attempt(model_id, label, _both)
            if ok:
                content = str(getattr(res, "content", res))[:90]
                line(label, f"{type(res).__name__}: {content!r}")
                record(model_id, label, f"OK — {type(res).__name__} {content!r}")

        # R2c — native tool_choice. `runner/turn/conductor.py` carries a whole
        # no-tools-retry workaround that exists ONLY because the text protocol
        # could not enforce `required`.
        #
        # The first attempt TIMED OUT (>90s) rather than erroring, which is
        # ambiguous — a stall is not the same as "unsupported". So: give it a
        # longer budget, prompt it where a tool call is NATURAL (forcing a tool
        # on "say hello" is a pathological ask that some models grind on), and
        # try the alternate spellings the API accepts.
        for label, choice in (
            ("R2c tool_choice=required (natural prompt)", "required"),
            ("R2c tool_choice=any", "any"),
            ("R2c tool_choice=named function", {
                "type": "function", "function": {"name": "get_weather"}}),
        ):
            async def _required(_c=choice):
                return await asyncio.wait_for(
                    m.bind(tools=[strict_tool], tool_choice=_c).ainvoke(
                        "What is the weather in Phoenix?"),
                    timeout=max(CALL_TIMEOUT_S, 180.0))

            ok, res = await attempt(model_id, label, _required)
            if ok:
                calls = getattr(res, "tool_calls", None)
                line(label, "ENFORCED" if calls else "NOT enforced (no call)")
                record(model_id, label,
                       "ENFORCED — conductor workaround can go" if calls
                       else "accepted but produced NO tool call")

        # R2d — parallel tool calls (specialist sets parallel_tool_calls=True).
        async def _parallel():
            return await m.bind_tools(
                [GetWeather], parallel_tool_calls=True).ainvoke(
                "Weather in Phoenix AND in Denver? Call the tool for each.")

        ok, res = await attempt(model_id, "R2d parallel_tool_calls", _parallel)
        if ok:
            calls = getattr(res, "tool_calls", None) or []
            line("parallel tool calls", f"{len(calls)} call(s)")
            record(model_id, "R2d parallel_tool_calls", f"{len(calls)} call(s) returned")

        # R2e — streaming with tool calls. The conductor drives the
        # orchestrator with Runner.run_streamed; the shim currently fakes a
        # single-chunk stream. If real streaming yields tool_call_chunks we
        # can stop faking it (and get incremental SSE for free).
        async def _stream():
            kinds, tool_chunks, n = [], 0, 0
            async for chunk in m.bind_tools([GetWeather]).astream(
                    "What is the weather in Phoenix? Use the tool."):
                n += 1
                kinds.append(type(chunk).__name__)
                if getattr(chunk, "tool_call_chunks", None):
                    tool_chunks += 1
            return n, tool_chunks, kinds[:3]

        ok, res = await attempt(model_id, "R2e streaming tool_call_chunks", _stream)
        if ok:
            n, tool_chunks, kinds = res
            line("astream", f"{n} chunks, {tool_chunks} with tool_call_chunks, {kinds}")
            record(model_id, "R2e streaming tool_call_chunks",
                   f"{n} chunks, {tool_chunks} carried tool_call_chunks"
                   + (" — REAL streaming" if n > 1 else " — single chunk only"))

    # ── Q5: firewall rejection shape (opt-in) ────────────────────────────
    if reject_probe:
        async def _reject():
            # Prefer the structured path — that is the one whose rejection
            # shape we don't know; fall back to a bare call otherwise.
            if _LANGCHAIN_OK and hasattr(m, "with_structured_output"):
                chain = (PromptTemplate.from_template("{q}")
                         | m.with_structured_output(ProbeShape))
                return await chain.ainvoke({"q": reject_probe})
            return await m.ainvoke(reject_probe)

        ok, res = await attempt(model_id, "firewall rejection", _reject)
        if ok:
            # No raise: the caller must detect rejection from the VALUE, which
            # is a different code path than today's 401/403/400 string matching.
            line("rejection", f"NO EXCEPTION — returned {type(res).__name__}: {res!r}"[:160])
            record(model_id, "firewall rejection",
                   f"returned (no raise): {type(res).__name__} {res!r}"[:140])


# ── main ────────────────────────────────────────────────────────────────────

def parse_args():
    """Parsed OUTSIDE the event loop — argparse exits via SystemExit for
    `--help` / bad input, and under nest_asyncio that surfaces from inside a
    coroutine as a noisy 'Task exception was never retrieved' traceback."""
    global CALL_TIMEOUT_S
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="*", default=None,
                    help=f"model ids to probe (default: {DEFAULT_MODELS})")
    ap.add_argument("--reject-probe", default=None,
                    help="text your firewall blocks, to capture the rejection shape")
    ap.add_argument("--only", default=None,
                    help="comma-separated substrings; run ONLY matching checks "
                         "(e.g. --only R2b,R2c) so a follow-up run doesn't "
                         "re-pay for checks that already answered")
    ap.add_argument("--timeout", type=float, default=None,
                    help=f"per-call seconds (default {CALL_TIMEOUT_S:.0f}); raise "
                         f"it when a check reports TIMEOUT")
    args = ap.parse_args()
    if args.timeout:
        CALL_TIMEOUT_S = args.timeout
    if args.only:
        ONLY.extend(s.strip() for s in args.only.split(",") if s.strip())
        print(f"filtering to checks matching: {ONLY}")
    return args


async def main(args) -> None:
    models = args.models or DEFAULT_MODELS

    print(f"safechain_probe {PROBE_REVISION}")
    print(f"python {sys.version.split()[0]}")
    try:
        import langchain_core
        print(f"langchain_core {getattr(langchain_core, '__version__', '?')}")
    except ImportError:
        print("langchain_core NOT importable")

    surface = probe_imports()
    if surface is None:
        return

    for model_id in models:
        try:
            await probe_model(model_id, surface, args.reject_probe)
        except Exception:  # noqa: BLE001 - never let one model kill the run
            print(f"\n  !! unhandled while probing {model_id}:")
            traceback.print_exc()

    print_timings(hdr)

    hdr(f"SUMMARY — paste this back  [probe {PROBE_REVISION}]")
    width = max((len(row[1]) for row in RESULTS), default=20)
    current = None
    for model_id, check, verdict in RESULTS:
        if model_id != current:
            print(f"\n[{model_id}]")
            current = model_id
        print(f"  {check:<{width}}  {verdict}")
    print()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
