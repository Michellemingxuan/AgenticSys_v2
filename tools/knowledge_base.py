"""Knowledge-base retrieval — "any other similar cases like this one?"

THE KNOWLEDGE BASE is built outside this repo: a pile of prior case reports,
each distilled into ~10 points, clustered into **common** characteristics
(patterns many cases share) and **unique** characteristics (patterns that
single a case out). Every characteristic carries the case_ids that exhibit it.
The platform exposes ONE entry point:

    answer_question(json_path, question, conversation_history, target_pattern)

returning ``answer`` (grounded prose), ``retrieval_query`` (what it decided to
search for), ``search_text`` (that blended with our ``target_pattern`` — what
it actually searched), ``matched_clusters`` (the characteristics it hit) and
``relevant_bullets`` (the per-case distilled points behind them, with quotes).

``_client_kwargs`` still binds by INTROSPECTION rather than calling those four
positionally: the signature moved twice during the handover (``target_pattern``
was added, ``conversation_history`` was briefly dropped), and a rigid call turns
any further drift into a TypeError that reads as "the knowledge base is broken".
Passing only what the callable declares is also what removes the need for a
hand-written adapter module — a delivered script works as-is.

Not to be confused with the session-local knowledge-POINT cache in
``tools/kp_tools.py`` (``kp_lookup`` / ``kp_list_topics``), which holds what
THIS case's specialists already found this session. KB = other cases,
KP = this case. The two are deliberately separate tools.

## Swapping in the real client

Nothing here hard-codes the platform's package, because it does not exist in
dev. The callable is resolved at CALL TIME from the environment, so production
is a config change and not a code change. Either form works — an installed
package, or the loose .py the platform is likelier to hand over:

    KNOWLEDGE_BASE_CLIENT=some_pkg.some_module:answer_question
    KNOWLEDGE_BASE_CLIENT=/abs/path/kb_answer.py:answer_question
    KNOWLEDGE_BASE_JSON=/abs/path/aggregated_rank_top_common_unique.json

Every knob, including the master switch, lives in ``config/tuning.yaml`` under
``knowledge_base:`` and reaches this module as the env vars above (an inline env
var still wins — see ``config/tuning_loader.py``).

Three states, and the difference between the last two is the whole point of
``is_enabled``:

* **on and working** — a result.
* **switched off** (``enabled: false``, or nothing configured) — ``status:
  "disabled"``, and the specialist is told to answer "not applicable". Off is a
  complete answer, not a fault; reporting it as a fault invites the model to
  compensate for it.
* **on but broken** (client missing, unreachable, timed out) — ``status:
  "unavailable"``, which is a misconfiguration the deployment should SEE.

In no state does it invent a similar case: a fabricated case_id is worse than a
missing one, because a reviewer cannot tell it apart from a real referral. Tests
inject a fake via ``set_knowledge_base_client``.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from agents import RunContextWrapper, function_tool


# How much of the KB's answer reaches the specialist's context. The full
# payload can carry dozens of bullets across many clusters; the specialist
# needs the shape of the pattern and enough case-level evidence to cite, not
# the whole retrieval.
_MAX_CLUSTERS = int(os.environ.get("KNOWLEDGE_BASE_MAX_CLUSTERS", "5"))
_MAX_BULLETS = int(os.environ.get("KNOWLEDGE_BASE_MAX_BULLETS", "12"))
_MAX_TEXT_CHARS = int(os.environ.get("KNOWLEDGE_BASE_TEXT_CHARS", "400"))
# The KB's own synthesis gets its own budget: measured payloads run ~2100 chars
# and it is the single most useful field, so it must not be trimmed to the same
# length as one bullet's quote.
_MAX_ANSWER_CHARS = int(os.environ.get("KNOWLEDGE_BASE_ANSWER_CHARS", "1600"))
# Turns of this session's conversation handed to the KB so it can resolve what
# "similar" refers to across a follow-up ("any others like that?").
_HISTORY_TURNS = int(os.environ.get("KNOWLEDGE_BASE_HISTORY_TURNS", "5"))
# The client is a REMOTE, possibly-sync call. It runs off the event loop (see
# `_call_client`) and under a timeout, because a specialist's whole answer has
# a ~20s budget and an unbounded retrieval would spend all of it.
_TIMEOUT_S = float(os.environ.get("KNOWLEDGE_BASE_TIMEOUT_S", "15"))

# Test/notebook override, set via `set_knowledge_base_client`. Takes precedence
# over KNOWLEDGE_BASE_CLIENT.
_CLIENT_OVERRIDE: Callable[..., Any] | None = None

# Modules loaded from a loose .py path, keyed by path — executing a script once
# per specialist call would re-pay its import cost on every question.
_PATH_MODULES: dict[str, Any] = {}

# The knowledge base gets its OWN small thread pool, never the shared default
# `to_thread` one. Two reasons, both observed rather than theoretical:
#
#   1. A sync call that overruns cannot be cancelled. `wait_for` gives up and we
#      answer "unavailable", but the thread keeps working — and the client
#      RETRIES internally on 429, so overruns are expected, not exceptional.
#      On the shared pool those orphans accumulate until the orchestrator
#      cannot get a thread, which is this repo's documented "stuck at team
#      construction" outage. Confined here, a wedged knowledge base degrades
#      only the knowledge base.
#   2. The client caches a safechain model across calls and is not known to be
#      thread-safe. One worker serialises access, and under rate limiting that
#      is what you want anyway — parallel specialists calling it at once is how
#      you earn the 429 in the first place.
#
# Raise `max_concurrency` only if the client is confirmed thread-safe AND the
# backend is not rate-limiting.
_MAX_CONCURRENCY = max(1, int(os.environ.get("KNOWLEDGE_BASE_MAX_CONCURRENCY", "1")))
_EXECUTOR: Any = None
_EXECUTOR_LOCK = threading.Lock()


def _executor():
    """The knowledge base's own worker pool, built on first use."""
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = ThreadPoolExecutor(
                    max_workers=_MAX_CONCURRENCY, thread_name_prefix="kb-client")
    return _EXECUTOR


async def _off_loop(fn: Callable[[], Any]) -> Any:
    """Run a blocking callable on the knowledge base's own pool."""
    return await asyncio.get_running_loop().run_in_executor(_executor(), fn)


def set_knowledge_base_client(fn: Callable[..., Any] | None) -> None:
    """Install (or clear, with None) the ``answer_question`` callable.

    For tests and notebooks. Production wires the real client through
    ``KNOWLEDGE_BASE_CLIENT`` instead, so no code imports the platform package.
    """
    global _CLIENT_OVERRIDE
    _CLIENT_OVERRIDE = fn


def _load_module_from_path(path: str):
    """Import a LOOSE .py file — the shape the platform is likely to hand over.

    A script dropped into the repo (or onto the server) is not an installed
    package, so `import_module` cannot see it. Loading it by file path means the
    handover needs no packaging and no wrapper module.

    Its DIRECTORY goes on sys.path first, because a delivered script imports its
    siblings — `retrieval.py` doing `from embeddings import EmbeddingService`.
    That resolves when you run the script directly, since Python puts the
    script's own directory on `sys.path[0]`, and fails under
    `spec_from_file_location`, which adds nothing. The symptom is a
    `ModuleNotFoundError` for a file sitting right next to the one that works
    standalone.

    APPENDED, not prepended — the opposite of what running a script does, and
    deliberately. Their directory may hold a `config.py`, `models.py`,
    `logger.py` or `tools.py`, and prepending would let it shadow ours for the
    rest of the process. Appending resolves their siblings while leaving every
    name this repo already owns untouched.

    Cached: the module is executed once, not once per specialist call.
    """
    cached = _PATH_MODULES.get(path)
    if cached is not None:
        return cached
    import importlib.util

    resolved = os.path.abspath(path)
    parent = os.path.dirname(resolved)
    if parent and parent not in sys.path:
        sys.path.append(parent)

    name = f"_kb_client_{abs(hash(resolved))}"
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: a module that imports itself, uses dataclasses, or
    # pickles anything expects to find itself in sys.modules while it runs.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)      # a failed import must leave no husk
        raise
    _PATH_MODULES[path] = module
    return module


def _resolve_client() -> tuple[Callable[..., Any] | None, str]:
    """Return ``(callable, problem)`` for the configured entry point.

    ``KNOWLEDGE_BASE_CLIENT`` takes either form:

        pkg.module:answer_question      an importable package
        /abs/path/kb_client.py:answer_question   a loose script

    The attribute defaults to ``answer_question`` when the ``:`` part is
    omitted. Resolved per call rather than at import: the env may be set after
    this module is imported (server bootstrap, notebook), and a missing package
    must degrade to "unavailable" rather than break the import graph.

    ``problem`` carries WHY resolution failed, because the failures are not
    interchangeable and one of them used to masquerade as another. Importing the
    target RUNS the module: a real client does `load_dotenv`, imports safechain,
    and may build a model at module scope. Any of that can raise — and reporting
    it as "KNOWLEDGE_BASE_CLIENT is unset" (which this did) sends whoever is
    debugging to the config, which is correct, while the actual exception is
    swallowed. Cheap to distinguish; expensive to guess.
    """
    if _CLIENT_OVERRIDE is not None:
        return _CLIENT_OVERRIDE, ""
    spec = (os.environ.get("KNOWLEDGE_BASE_CLIENT") or "").strip()
    if not spec:
        return None, "KNOWLEDGE_BASE_CLIENT is unset"
    # rpartition, not partition: a Windows-style or absolute path may itself
    # contain no colon, but splitting from the RIGHT keeps `/a/b.py:fn` intact.
    target, sep, attr = spec.rpartition(":")
    if not sep:
        target, attr = spec, ""
    attr = attr or "answer_question"
    try:
        if target.endswith(".py") or os.sep in target:
            module = _load_module_from_path(target)
        else:
            module = importlib.import_module(target)
    except Exception as exc:  # noqa: BLE001 — a bad config must not kill the turn
        return None, (f"importing {target!r} raised "
                      f"{type(exc).__name__}: {str(exc)[:300]}")
    if module is None:
        return None, f"{target!r} could not be loaded as a Python module"
    fn = getattr(module, attr, None)
    if fn is None:
        return None, (f"{target!r} imported, but has no attribute {attr!r} — "
                      f"check the name after the ':'")
    if not callable(fn):
        return None, f"{target}:{attr} is not callable"
    return fn, ""


def _client_kwargs(fn: Callable[..., Any], *, json_path: str, question: str,
                   history: list[dict], target_pattern: str) -> dict | None:
    """Bind our arguments to whatever parameters the client actually declares.

    The platform's signature is not fully settled — `conversation_history` may
    not survive, `target_pattern` may appear — and a rigid keyword call would
    turn every such difference into a TypeError that reads as "the knowledge
    base is broken". So pass what it asks for and drop the rest.

    Returns None when the callable does not take a `question`, which means the
    configured attribute is not the entry point we think it is; the caller
    reports that with the parameter names it DID find, so the misconfiguration
    is diagnosable from one log line instead of a stack trace.
    """
    try:
        params = inspect.signature(fn).parameters
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            accepted = None                      # **kwargs — send everything
        else:
            accepted = {
                name for name, p in params.items()
                if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.KEYWORD_ONLY)
            }
            if "question" not in accepted:
                return None
    except (TypeError, ValueError):
        accepted = None                          # un-introspectable: send everything

    available = {
        "json_path": json_path,
        "question": question,
        "conversation_history": history,
        "target_pattern": target_pattern,
    }
    # The pattern is the whole point of the call — it is what the specialist
    # pivoted to. If this client has no parameter for it, it must not be
    # silently dropped: fold it into the question so a client built on the
    # earlier (question, conversation_history) spec still retrieves on it.
    if target_pattern and accepted is not None and "target_pattern" not in accepted:
        available["question"] = (
            f"{question}\n\nPattern to retrieve: {target_pattern}"
        )
    if accepted is None:
        return available
    return {k: v for k, v in available.items() if k in accepted}


def _json_path() -> str:
    return (os.environ.get("KNOWLEDGE_BASE_JSON") or "").strip()


_FALSEY = frozenset({"0", "false", "no", "off", "none", ""})


def is_enabled() -> bool:
    """Master switch — ``KNOWLEDGE_BASE_ENABLED``, from config/tuning.yaml.

    Three states, because "off" and "broken" must not look alike to a reviewer:

      false → OFF. The tool answers "not applicable" and calls nothing.
      true  → ON. A missing client / json_path then reports "unavailable",
              which is a misconfiguration worth SEEING rather than a quiet no-op.
      unset → AUTO: on when a client and a json_path are both configured.
              So a dev box with nothing wired reads as "off", not as "broken",
              while setting the two variables is enough to turn it on.

    Read at call time (not import) so a test or notebook can flip it.
    """
    raw = os.environ.get("KNOWLEDGE_BASE_ENABLED")
    if raw is not None:
        return raw.strip().lower() not in _FALSEY
    return bool(_CLIENT_OVERRIDE or
                ((os.environ.get("KNOWLEDGE_BASE_CLIENT") or "").strip()
                 and _json_path()))


def build_conversation_history(episodic_records: Any,
                               limit: int | None = None) -> list[dict]:
    """Episodic records → the KB's ``conversation_history`` shape.

    ``AppContext._episodic_records`` is NEWEST-FIRST (see tools/episodic.py);
    the KB expects chronological order, so the newest `limit` turns are taken
    and then reversed. Records missing a question are skipped — a turn with no
    question cannot help resolve what "similar" refers to.

    ``turn_id`` is renumbered "1", "2", … rather than passed through. Our real
    turn ids are random hex; the KB's own examples use ordinals, and an ordinal
    is also MONOTONIC — so if anything downstream sorts or indexes by turn_id
    it still gets the conversation in the order it happened.
    """
    limit = _HISTORY_TURNS if limit is None else limit
    if not isinstance(episodic_records, list):
        return []
    out: list[dict] = []
    for rec in episodic_records[:max(0, limit)]:
        if not isinstance(rec, dict):
            continue
        question = (rec.get("question") or "").strip()
        if not question:
            continue
        out.append({
            "question": question,
            "answer": (rec.get("final_answer") or "").strip(),
        })
    out.reverse()
    return [{"turn_id": str(i), **turn} for i, turn in enumerate(out, start=1)]


# Defaults resolve at CALL time, not at def time: the caps are module
# globals an operator (or a test) may rebind after import.
def _trim(value: Any, limit: int | None = None) -> str:
    limit = _MAX_TEXT_CHARS if limit is None else limit
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _round(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _interleave_by_cluster(bullets: list[dict], limit: int) -> list[dict]:
    """Take `limit` bullets ROUND-ROBIN across clusters, not off the top.

    Measured against a real payload: three clusters scored 0.594 / 0.591 /
    0.580, and the one that actually answered the question ("reliance on minimum
    payments", for a revolving-balance query) ranked LAST. The scores are that
    tightly packed throughout, so head-truncation is close to arbitrary — and it
    systematically starves the lowest-ranked cluster, which is exactly where the
    on-target evidence sat. Round-robin degrades every cluster evenly instead.
    """
    groups: dict[str, list[dict]] = {}
    for b in bullets:
        if isinstance(b, dict):
            groups.setdefault(str(b.get("cluster_key") or ""), []).append(b)
    out: list[dict] = []
    rank = 0
    while len(out) < limit and any(len(g) > rank for g in groups.values()):
        for group in groups.values():
            if len(group) > rank:
                out.append(group[rank])
                if len(out) >= limit:
                    break
        rank += 1
    return out


def summarize_kb_result(payload: Any, self_case_id: str | None = None) -> dict:
    """Platform payload → the compact dict the specialist reads.

    Keeps what a specialist can actually USE — the pattern, which cases carry
    it, and a quotable point per case — and drops the retrieval bookkeeping
    (per-cluster sub-scores, cluster keys). ``similar_cases`` is lifted to the
    top because it is the literal answer to "any other similar cases?".

    ``self_case_id`` is the case under review. Its report may well be IN the
    corpus the knowledge base was built from — case_ids are bare digit strings,
    indistinguishable from ours — and then it comes back as its own "similar
    case". That is not an answer to "any OTHER similar cases", and a reviewer
    reading their own case id cited back at them as a precedent has been
    actively misled, so it is dropped here and the drop is reported.
    """
    if not isinstance(payload, dict):
        return {"status": "unavailable",
                "note": "knowledge base returned an unrecognized payload"}

    clusters = payload.get("matched_clusters")
    bullets = payload.get("relevant_bullets")
    clusters = clusters if isinstance(clusters, list) else []
    bullets = bullets if isinstance(bullets, list) else []

    self_id = (str(self_case_id).strip() if self_case_id else "")
    excluded_self = False
    if self_id:
        kept = [b for b in bullets
                if not (isinstance(b, dict)
                        and str(b.get("case_id") or "").strip() == self_id)]
        excluded_self = len(kept) != len(bullets)
        bullets = kept

    # case_ids per cluster live on the bullets, so index them first.
    cases_by_cluster: dict[str, list[str]] = {}
    for b in bullets:
        if not isinstance(b, dict):
            continue
        key = str(b.get("cluster_key") or "")
        case_id = str(b.get("case_id") or "").strip()
        if not case_id:
            continue
        seen = cases_by_cluster.setdefault(key, [])
        if case_id not in seen:
            seen.append(case_id)

    patterns = []
    for c in clusters[:_MAX_CLUSTERS]:
        if not isinstance(c, dict):
            continue
        patterns.append({
            "pattern_type": c.get("pattern_type"),
            "pattern": _trim(c.get("cluster_text")),
            "score": _round(c.get("final_score")),
            "cases": cases_by_cluster.get(str(c.get("cluster_key") or ""), []),
        })

    evidence = []
    for b in _interleave_by_cluster(bullets, _MAX_BULLETS):
        entry = {
            "case_id": b.get("case_id"),
            "pattern_type": b.get("pattern_type"),
            "point": _trim(b.get("text")),
            "similarity": _round(b.get("similarity")),
        }
        # `rationale` runs ~340 chars and is the KB's commentary on its OWN
        # selection — why that bullet was written into that other case's report.
        # Useful for orientation, but it is the least load-bearing field for a
        # specialist that has to cite something, so it gets half the budget.
        rationale = _trim(b.get("rationale"), max(1, _MAX_TEXT_CHARS // 2))
        if rationale:
            entry["why"] = rationale
        quote = _trim(b.get("raw_quote"))
        if quote:
            entry["quote"] = quote
        evidence.append(entry)

    all_cases: list[str] = []
    for b in bullets:
        if isinstance(b, dict):
            case_id = str(b.get("case_id") or "").strip()
            if case_id and case_id not in all_cases:
                all_cases.append(case_id)

    retrieval_query = _trim(payload.get("retrieval_query"), 200)
    result: dict[str, Any] = {
        "status": "ok",
        "similar_cases": all_cases,
        "answer": _trim(payload.get("answer"), _MAX_ANSWER_CHARS),
        "retrieval_query": retrieval_query,
        "patterns": patterns,
        "evidence": evidence,
    }
    # What the KB actually searched on — `retrieval_query` blended with the
    # `target_pattern` we passed. Only worth context when it differs, but then
    # it is what tells the specialist whether a weak result came from a weak
    # pattern (and so whether re-asking on another axis is worth a round).
    search_text = _trim(payload.get("search_text"), 200)
    if search_text and search_text != retrieval_query:
        result["search_text"] = search_text
    if len(clusters) > len(patterns):
        result["patterns_omitted"] = len(clusters) - len(patterns)
    if len(bullets) > len(evidence):
        result["evidence_omitted"] = len(bullets) - len(evidence)
    if excluded_self:
        # Say it, don't just do it: silently shrinking the evidence would leave
        # the specialist unable to explain why a case it can see in this session
        # is absent from the comparison.
        result["excluded_self_case"] = self_id
    if not all_cases:
        result["note"] = (
            "the knowledge base matched no prior case for this framing — say "
            "so; do not substitute a case from memory"
        )
    return result


async def _call_client(fn: Callable[..., Any], kwargs: dict) -> Any:
    """Invoke the platform callable, sync or async, off the event loop.

    A sync client is a BLOCKING call. Run inline it would hold the loop for the
    length of the retrieval, which does not merely make this tool slow: it
    makes every concurrent specialist slow and the turn uncancellable while it
    runs (the same failure mode as the tiktoken download — see
    `.claude/memory/safechain_async_and_thread_occupation.md`). So it goes to a
    worker thread, and the whole thing runs under a timeout.
    """
    coro = (fn(**kwargs) if inspect.iscoroutinefunction(fn)
            else _off_loop(lambda: fn(**kwargs)))
    return await asyncio.wait_for(coro, timeout=_TIMEOUT_S)


def _looks_rate_limited(exc: Exception) -> bool:
    """429 is not a fault to fix, it is a backend asking for less traffic.

    Worth separating from a generic failure: the response is to lower
    concurrency or widen the timeout, not to debug the client. Matched on the
    message because the client wraps the HTTP error in its own retry logic and
    the status code does not survive as a typed attribute."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


_UNAVAILABLE_NOTE = (
    "Answer from this case's own data instead, and say plainly that no "
    "comparable-case lookup was available. Do NOT name a similar case from "
    "memory — an invented case_id is indistinguishable from a real referral."
)

# The OFF state is not a failure and must not be reported as one. "The lookup
# failed" invites the model to compensate — to reach for a case it half-recalls,
# or to hedge at length about a capability the deployment simply does not have.
# "Not applicable" closes that door: it is a complete answer to the comparison,
# and it leaves the rest of the question to this case's own data.
_DISABLED_NOTE = (
    "Comparison against prior cases is NOT ENABLED in this deployment. Answer "
    "the comparison part of the question with \"not applicable\" — state it "
    "plainly and briefly, do not apologise for it and do not describe it as a "
    "failure. Answer any remaining part of the question from this case's own "
    "data. Do NOT name a similar case from memory: with no knowledge base "
    "wired, any case_id you produce is invented."
)


@function_tool
async def knowledge_base_search(ctx: RunContextWrapper, question: str,
                                target_pattern: str) -> str:
    """Search the internal knowledge base of PRIOR CASES for ones that resemble
    this case, and return the matching patterns with their case_ids.

    Use for "any other similar cases like this one?", "has this happened
    before?", "is this pattern common or unusual?", "what usually precedes X?".

    Two arguments, and the SECOND is the one that decides what comes back:

    `question` — what is actually being asked, in the reviewer's terms (your
    sub-question is usually it verbatim): "any other similar cases like this
    one?", "is this normal?".

    `target_pattern` — the concrete behavioural pattern to retrieve on. The
    reviewer almost never states what "similar" means, so YOU pivot it from
    what this case shows: your findings this turn, the cached knowledge points,
    the conversation so far. Name the shape and its outcome — "revolving
    balance near limit for 6+ months with minimum-due-only payments, ending in
    default" — never "cases similar to this one". A vague pattern retrieves
    vague clusters, which is the main way this tool wastes a round.

    Returns JSON: `similar_cases` (case_ids), `answer`, `patterns` (each a
    common or unique characteristic, with the cases carrying it), and
    `evidence` (the distilled point per case, with quotes). `search_text`,
    when present, is what was actually searched — judge a thin result
    against it before re-asking. Scores RANK the matches, they do not
    certify them: read each `pattern` and, if none is on target, say the
    knowledge base found no close analogue rather than reporting the
    highest-scoring one.

    `status: "disabled"` means prior-case comparison is not enabled in this
    deployment: answer that part with "not applicable", briefly and without
    apology, and answer the rest from this case's data. Cite case_ids and
    patterns as PRIOR-CASE context, never as facts about this case, and never
    name a case the tool did not return."""
    app_ctx: Any = ctx.context if ctx else None
    logger = getattr(app_ctx, "logger", None)
    query = (question or "").strip()
    pattern = (target_pattern or "").strip()

    if logger is not None:
        logger.log("tool_call", {"tool": "knowledge_base_search",
                                 "args": {"question": query[:200],
                                          "target_pattern": pattern[:200]}})

    # Checked FIRST, before the empty-argument guard and before any client
    # resolution: when the feature is off, nothing about how the call was
    # phrased matters, and the answer must be the same every time.
    if not is_enabled():
        if logger is not None:
            logger.log("tool_result", {"tool": "knowledge_base_search",
                                       "status": "disabled"})
        return json.dumps({"status": "disabled",
                           "answer": "not applicable",
                           "note": _DISABLED_NOTE})

    if not query and not pattern:
        return json.dumps({
            "status": "unavailable",
            "note": "pass the concrete behavioural pattern you want matched in "
                    "`target_pattern`; an empty call retrieves nothing",
        })

    json_path = _json_path()
    if not json_path:
        out = {"status": "unavailable",
               "note": f"the knowledge base is not configured here "
                       f"(KNOWLEDGE_BASE_JSON is unset). {_UNAVAILABLE_NOTE}"}
        if logger is not None:
            logger.log("tool_result", {"tool": "knowledge_base_search",
                                       "status": "unavailable",
                                       "missing": "KNOWLEDGE_BASE_JSON"})
        return json.dumps(out)

    # Resolution goes OFF THE LOOP and under the timeout, because importing the
    # target EXECUTES it: a real client's module scope does `load_dotenv`,
    # imports safechain, and may build a model — seconds of blocking work, some
    # of it network I/O. Run inline it would freeze every concurrent specialist
    # and make the turn uncancellable while it ran, which is the exact failure
    # shape in `.claude/memory/safechain_async_and_thread_occupation.md`. The
    # module is cached after the first success, so this is paid once.
    try:
        fn, problem = await asyncio.wait_for(
            _off_loop(_resolve_client), timeout=_TIMEOUT_S)
    except asyncio.TimeoutError:
        fn, problem = None, (f"importing the client did not finish within "
                             f"{_TIMEOUT_S:.0f}s")
    if fn is None:
        out = {"status": "unavailable",
               "note": f"the knowledge base client could not be loaded: "
                       f"{problem}. {_UNAVAILABLE_NOTE}"}
        if logger is not None:
            logger.log("tool_result", {"tool": "knowledge_base_search",
                                       "status": "unresolved",
                                       "problem": problem})
        return json.dumps(out)

    history = build_conversation_history(
        getattr(app_ctx, "_episodic_records", None))
    kwargs = _client_kwargs(fn, json_path=json_path, question=query,
                            history=history, target_pattern=pattern)
    if kwargs is None:
        # The configured attribute exists but is not the entry point — name what
        # it DID declare, so this is one readable log line, not a stack trace.
        try:
            found = ", ".join(inspect.signature(fn).parameters) or "(none)"
        except (TypeError, ValueError):
            found = "(uninspectable)"
        out = {"status": "unavailable",
               "note": f"the configured knowledge-base callable takes no "
                       f"`question` parameter (it declares: {found}). "
                       f"{_UNAVAILABLE_NOTE}"}
        if logger is not None:
            logger.log("tool_result", {"tool": "knowledge_base_search",
                                       "status": "bad_signature",
                                       "params": found})
        return json.dumps(out)

    started = time.monotonic()
    try:
        payload = await _call_client(fn, kwargs)
    except asyncio.TimeoutError:
        out = {"status": "unavailable",
               "note": f"the knowledge base did not respond within "
                       f"{_TIMEOUT_S:.0f}s (it may be retrying against a "
                       f"rate-limited backend). {_UNAVAILABLE_NOTE}"}
        if logger is not None:
            logger.log("tool_result", {"tool": "knowledge_base_search",
                                       "status": "timeout",
                                       "ms": int((time.monotonic() - started) * 1000)})
        return json.dumps(out)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a KB fault must not fail the turn
        # Deliberately NOT an "error" key: `grounding._classify_scalar` reads
        # that as a rejected data call and quarantines the specialist's whole
        # answer. A KB miss is a missing OPTION, not a broken measurement.
        rate_limited = _looks_rate_limited(exc)
        detail = ("the knowledge base is rate-limited (429) and its retries did "
                  "not clear it" if rate_limited else
                  f"the knowledge base lookup failed ({type(exc).__name__})")
        out = {"status": "unavailable", "note": f"{detail}. {_UNAVAILABLE_NOTE}"}
        if logger is not None:
            logger.log("tool_result", {"tool": "knowledge_base_search",
                                       "status": "rate_limited" if rate_limited
                                                 else "failed",
                                       "exc_type": type(exc).__name__,
                                       "message": str(exc)[:300]})
        return json.dumps(out)

    result = summarize_kb_result(
        payload, self_case_id=getattr(app_ctx, "_case_id", None))
    if logger is not None:
        logger.log("tool_result", {
            "tool": "knowledge_base_search",
            "status": result.get("status"),
            "target_pattern": pattern[:200],
            "retrieval_query": result.get("retrieval_query"),
            "search_text": result.get("search_text"),
            "n_cases": len(result.get("similar_cases") or []),
            "n_patterns": len(result.get("patterns") or []),
            "n_evidence": len(result.get("evidence") or []),
            "ms": int((time.monotonic() - started) * 1000),
        })
    return json.dumps(result, default=str)
