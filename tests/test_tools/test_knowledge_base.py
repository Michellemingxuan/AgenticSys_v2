"""knowledge_base_search — the tool specialists use to find PRIOR cases that
resemble this one.

The real `answer_question` client does not exist in dev (see
`skills/helper/knowledge_base.md`), so every happy-path test injects a fake via
`set_knowledge_base_client`. What the tests pin is the CONTRACT the specialist
depends on: an unavailable knowledge base never fabricates a match, a failure
never looks like a broken data query, and the payload is compacted without
losing the case_ids.
"""

import asyncio
import json
import pathlib
import types

import pytest

from tools import knowledge_base as kb
from tools.knowledge_base import summarize_kb_result


# The platform's documented output shape.
_PAYLOAD = {
    "answer": "Prior cases show revolving balance climbing for months first.",
    "retrieval_query": "persistent revolving balance leading to default",
    "search_text": "persistent revolving balance leading to default | balance near limit for 6+ months",
    "matched_clusters": [
        {"cluster_key": "common:12", "pattern_type": "common",
         "cluster_text": "Balance near limit for 6+ months before default",
         "cluster_similarity": 0.81, "bullet_cluster_score": 0.77,
         "final_score": 0.786},
        {"cluster_key": "unique:3", "pattern_type": "unique",
         "cluster_text": "Single-merchant concentration above 60%",
         "final_score": 0.62},
    ],
    "relevant_bullets": [
        {"cluster_key": "common:12", "pattern_type": "common",
         "cluster_text": "Balance near limit for 6+ months before default",
         "case_id": "case_123", "text": "Balance held at 95% of limit",
         "rationale": "Signals no repayment capacity",
         "similarity": 0.79, "raw_quote": "The account remained at 95% …"},
        {"cluster_key": "unique:3", "pattern_type": "unique",
         "cluster_text": "Single-merchant concentration above 60%",
         "case_id": "case_456", "text": "68% of spend at one merchant",
         "similarity": 0.66, "raw_quote": "Spend concentrated at …"},
    ],
}


def _ctx(episodic=None, logger=None):
    app_ctx = types.SimpleNamespace(
        _episodic_records=episodic if episodic is not None else [],
        logger=logger,
    )
    return types.SimpleNamespace(context=app_ctx)


def _invoke(question: str, pattern: str = "a behavioural pattern",
            ctx=None) -> dict:
    """Call the function_tool's underlying implementation and parse its JSON."""
    out = asyncio.run(
        kb.knowledge_base_search.on_invoke_tool(
            ctx if ctx is not None else _ctx(),
            json.dumps({"question": question, "target_pattern": pattern}),
        )
    )
    return json.loads(out)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_BASE_CLIENT", raising=False)
    monkeypatch.delenv("KNOWLEDGE_BASE_ENABLED", raising=False)
    monkeypatch.setenv("KNOWLEDGE_BASE_JSON", "/fake/aggregated.json")
    kb.set_knowledge_base_client(None)
    kb._PATH_MODULES.clear()
    yield
    kb.set_knowledge_base_client(None)
    kb._PATH_MODULES.clear()


# ── unavailable: the failure mode that must NEVER invent a case ─────────────

def test_missing_json_path_while_enabled_is_unavailable(monkeypatch):
    """ON but half-configured is a MISCONFIGURATION — it must be visible, not
    silently equivalent to the feature being switched off."""
    monkeypatch.setenv("KNOWLEDGE_BASE_ENABLED", "1")
    kb.set_knowledge_base_client(lambda **kw: _PAYLOAD)
    monkeypatch.delenv("KNOWLEDGE_BASE_JSON", raising=False)
    out = _invoke("anything")
    assert out["status"] == "unavailable"
    assert "KNOWLEDGE_BASE_JSON" in out["note"]


def test_missing_client_while_enabled_is_unavailable(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BASE_ENABLED", "1")
    kb.set_knowledge_base_client(None)
    out = _invoke("anything")
    assert out["status"] == "unavailable"
    assert "KNOWLEDGE_BASE_CLIENT" in out["note"]


def test_client_exception_is_reported_not_raised():
    def _boom(**kwargs):
        raise RuntimeError("kb backend down")

    kb.set_knowledge_base_client(_boom)
    out = _invoke("revolving balance to default")
    assert out["status"] == "unavailable"
    assert "RuntimeError" in out["note"]


def test_failure_payload_carries_no_error_key():
    """`grounding._classify_scalar` treats an `"error"` key in a tool result as
    a REJECTED data call and quarantines the specialist's whole answer. A
    knowledge-base miss is a missing option, not a broken measurement — so the
    failure shape must stay clear of that marker."""
    def _boom(**kwargs):
        raise RuntimeError("nope")

    kb.set_knowledge_base_client(_boom)
    raw = asyncio.run(kb.knowledge_base_search.on_invoke_tool(
        _ctx(), json.dumps({"question": "q", "target_pattern": "p"})))
    assert '"error"' not in raw
    assert "COLUMN NOT FOUND" not in raw and "did NOT run" not in raw


def test_timeout_degrades_to_unavailable(monkeypatch):
    monkeypatch.setattr(kb, "_TIMEOUT_S", 0.05)

    async def _slow(**kwargs):
        await asyncio.sleep(5)

    kb.set_knowledge_base_client(_slow)
    out = _invoke("slow one")
    assert out["status"] == "unavailable"
    assert "did not respond" in out["note"]


def test_empty_call_is_refused_without_reaching_the_client():
    calls = []

    def _client(**kwargs):
        calls.append(kwargs)
        return _PAYLOAD

    kb.set_knowledge_base_client(_client)
    out = _invoke("   ", "  ")
    assert out["status"] == "unavailable"
    assert calls == []


# ── happy path ──────────────────────────────────────────────────────────────

def test_returns_case_ids_patterns_and_evidence():
    kb.set_knowledge_base_client(lambda **kw: _PAYLOAD)
    out = _invoke("revolving balance near limit ending in default")

    assert out["status"] == "ok"
    assert out["similar_cases"] == ["case_123", "case_456"]
    assert out["retrieval_query"].startswith("persistent revolving balance")
    assert [p["pattern_type"] for p in out["patterns"]] == ["common", "unique"]
    # case_ids are indexed onto their cluster, so a pattern names its cases.
    assert out["patterns"][0]["cases"] == ["case_123"]
    assert out["evidence"][0]["case_id"] == "case_123"
    assert out["evidence"][0]["quote"].startswith("The account remained")
    assert out["evidence"][0]["why"] == "Signals no repayment capacity"


def test_question_pattern_and_json_path_reach_the_client():
    seen = {}

    def _client(**kwargs):
        seen.update(kwargs)
        return _PAYLOAD

    kb.set_knowledge_base_client(_client)
    _invoke("any other similar cases?", "merchant concentration before delinquency")
    assert seen["question"] == "any other similar cases?"
    assert seen["target_pattern"] == "merchant concentration before delinquency"
    assert seen["json_path"] == "/fake/aggregated.json"
    assert seen["conversation_history"] == []


def test_conversation_history_matches_the_documented_shape():
    """`_episodic_records` is newest-first (tools/episodic.py); the knowledge
    base expects the conversation in the order it happened, with ordinal
    turn_ids — the shape in the platform's own example."""
    seen = {}

    def _client(**kwargs):
        seen.update(kwargs)
        return _PAYLOAD

    kb.set_knowledge_base_client(_client)
    ctx = _ctx(episodic=[
        {"turn_id": "9f3a1c", "question": "What is its population?",
         "final_answer": "Approximately 2.1 million."},
        {"turn_id": "4b7e02", "question": "What is the capital of France?",
         "final_answer": "Paris."},
    ])
    _invoke("similar cases", ctx=ctx)

    assert seen["conversation_history"] == [
        {"turn_id": "1", "question": "What is the capital of France?",
         "answer": "Paris."},
        {"turn_id": "2", "question": "What is its population?",
         "answer": "Approximately 2.1 million."},
    ]


def test_history_window_is_bounded():
    """Newest 3 of 20, oldest-first, renumbered from 1."""
    records = [{"turn_id": f"t{i}", "question": f"q{i}", "final_answer": "a"}
               for i in range(20)]
    history = kb.build_conversation_history(records, limit=3)
    assert [h["question"] for h in history] == ["q2", "q1", "q0"]
    assert [h["turn_id"] for h in history] == ["1", "2", "3"]


def test_history_skips_records_without_a_question():
    records = [{"turn_id": "t1", "final_answer": "a"},
               {"turn_id": "t0", "question": "real", "final_answer": "a"}]
    history = kb.build_conversation_history(records)
    assert [h["question"] for h in history] == ["real"]


def test_no_match_is_a_result_not_a_failure():
    kb.set_knowledge_base_client(
        lambda **kw: {"answer": "", "retrieval_query": "x",
                      "matched_clusters": [], "relevant_bullets": []})
    out = _invoke("something with no prior analogue")
    assert out["status"] == "ok"
    assert out["similar_cases"] == []
    assert "matched no prior case" in out["note"]


# ── compaction: the specialist's context is not free ────────────────────────

def test_large_payload_is_capped_and_says_what_it_dropped(monkeypatch):
    monkeypatch.setattr(kb, "_MAX_CLUSTERS", 2)
    monkeypatch.setattr(kb, "_MAX_BULLETS", 3)
    payload = {
        "answer": "a", "retrieval_query": "q",
        "matched_clusters": [{"cluster_key": f"common:{i}",
                              "pattern_type": "common",
                              "cluster_text": f"pattern {i}",
                              "final_score": 0.5} for i in range(6)],
        "relevant_bullets": [{"cluster_key": f"common:{i}",
                              "pattern_type": "common",
                              "case_id": f"case_{i}", "text": f"point {i}",
                              "similarity": 0.5} for i in range(9)],
    }
    kb.set_knowledge_base_client(lambda **kw: payload)
    out = _invoke("broad question")

    assert len(out["patterns"]) == 2
    assert len(out["evidence"]) == 3
    assert out["patterns_omitted"] == 4
    assert out["evidence_omitted"] == 6
    # Every matched case still reaches the specialist — the caps trim the
    # supporting detail, not the answer to "which cases".
    assert len(out["similar_cases"]) == 9


def test_long_text_is_truncated(monkeypatch):
    monkeypatch.setattr(kb, "_MAX_TEXT_CHARS", 20)
    payload = {
        "answer": "a", "retrieval_query": "q",
        "matched_clusters": [],
        "relevant_bullets": [{"cluster_key": "c", "pattern_type": "common",
                              "case_id": "case_1", "text": "x" * 500,
                              "similarity": 0.5}],
    }
    kb.set_knowledge_base_client(lambda **kw: payload)
    out = _invoke("q")
    assert len(out["evidence"][0]["point"]) <= 21  # 20 + ellipsis


def test_unrecognized_payload_is_unavailable():
    kb.set_knowledge_base_client(lambda **kw: "not a dict")
    out = _invoke("q")
    assert out["status"] == "unavailable"


# ── wiring ──────────────────────────────────────────────────────────────────

def test_tool_is_strict():
    """Non-strict tools break the whole turn on safechain — see
    `test_every_tool_a_specialist_can_call_is_strict`."""
    assert kb.knowledge_base_search.strict_json_schema is True


def test_client_resolves_from_the_environment(monkeypatch):
    """Production wires the platform client by config, not by import."""
    monkeypatch.setenv("KNOWLEDGE_BASE_CLIENT", "json:dumps")
    kb.set_knowledge_base_client(None)
    assert kb._resolve_client() is json.dumps


def test_bad_client_spec_degrades_to_unavailable(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BASE_CLIENT", "no_such_module_xyz:answer_question")
    kb.set_knowledge_base_client(None)
    assert kb._resolve_client() is None
    out = _invoke("q")
    assert out["status"] == "unavailable"


def test_logger_records_the_call_and_the_result():
    events = []
    logger = types.SimpleNamespace(log=lambda name, payload: events.append((name, payload)))
    kb.set_knowledge_base_client(lambda **kw: _PAYLOAD)
    _invoke("revolving balance", ctx=_ctx(logger=logger))

    names = [n for n, _ in events]
    assert names == ["tool_call", "tool_result"]
    assert events[1][1]["n_cases"] == 2
    assert events[1][1]["status"] == "ok"


# ── taking the platform's script as it arrives ──────────────────────────────
#
# What lands is expected to be a loose .py taking (json_path, question,
# target_pattern) — not an installed package, and not necessarily carrying
# `conversation_history`. Both are absorbed here rather than in a wrapper
# module the integrator has to write and maintain.

_SCRIPT = """
CALLS = []

def answer_question(json_path, question, target_pattern):
    CALLS.append((json_path, question, target_pattern))
    return {"answer": "from the script", "retrieval_query": target_pattern,
            "matched_clusters": [],
            "relevant_bullets": [{"cluster_key": "c", "pattern_type": "common",
                                  "case_id": "case_9", "text": "point",
                                  "similarity": 0.5}]}
"""


def test_a_loose_script_is_loaded_by_file_path(tmp_path, monkeypatch):
    script = tmp_path / "kb_client.py"
    script.write_text(_SCRIPT)
    monkeypatch.setenv("KNOWLEDGE_BASE_CLIENT", f"{script}:answer_question")

    out = _invoke("any similar cases?", "balance near limit for 6+ months")

    assert out["status"] == "ok"
    assert out["similar_cases"] == ["case_9"]
    module = kb._PATH_MODULES[str(script)]
    assert module.CALLS == [("/fake/aggregated.json", "any similar cases?",
                             "balance near limit for 6+ months")]


def test_a_loose_script_is_executed_once_not_per_call(tmp_path, monkeypatch):
    script = tmp_path / "kb_client.py"
    script.write_text(_SCRIPT)
    monkeypatch.setenv("KNOWLEDGE_BASE_CLIENT", f"{script}:answer_question")

    first = kb._resolve_client()
    assert kb._resolve_client() is first          # cached module, same function


def test_attr_defaults_to_answer_question(tmp_path, monkeypatch):
    script = tmp_path / "kb_client.py"
    script.write_text(_SCRIPT)
    monkeypatch.setenv("KNOWLEDGE_BASE_CLIENT", str(script))
    assert kb._resolve_client() is not None


# ── signature binding ───────────────────────────────────────────────────────

def test_unknown_parameters_are_not_passed():
    """A client without `conversation_history` must not get one — a rigid
    keyword call would raise TypeError and read as "the KB is broken"."""
    seen = {}

    def _client(json_path, question, target_pattern):
        seen.update(json_path=json_path, question=question,
                    target_pattern=target_pattern)
        return _PAYLOAD

    kb.set_knowledge_base_client(_client)
    out = _invoke("q", "p")
    assert out["status"] == "ok"
    assert set(seen) == {"json_path", "question", "target_pattern"}


def test_pattern_folds_into_the_question_when_the_client_has_no_slot_for_it():
    """The pattern is what the specialist pivoted to — it is the whole point of
    the call, so a client built on the earlier (question, conversation_history)
    spec still receives it rather than silently losing it."""
    seen = {}

    def _client(json_path, question, conversation_history):
        seen.update(question=question)
        return _PAYLOAD

    kb.set_knowledge_base_client(_client)
    _invoke("any similar cases?", "balance near limit for 6+ months")
    assert "any similar cases?" in seen["question"]
    assert "balance near limit for 6+ months" in seen["question"]


def test_var_keyword_client_receives_everything():
    seen = {}

    def _client(**kwargs):
        seen.update(kwargs)
        return _PAYLOAD

    kb.set_knowledge_base_client(_client)
    _invoke("q", "p")
    assert set(seen) == {"json_path", "question", "conversation_history",
                         "target_pattern"}


def test_wrong_callable_names_the_parameters_it_actually_declares():
    """Pointing the env at the wrong attribute is the likeliest handover
    mistake; it must produce one readable line, not a TypeError."""
    def _not_the_entry_point(path, query):
        return {}

    kb.set_knowledge_base_client(_not_the_entry_point)
    out = _invoke("q", "p")
    assert out["status"] == "unavailable"
    assert "path, query" in out["note"]


# ── the platform's four-argument signature, as delivered ────────────────────

def test_the_delivered_signature_receives_all_four_arguments():
    """The shipped client declares (json_path, question, conversation_history,
    target_pattern) — every one of them is filled, and none is folded away."""
    seen = {}

    def answer_question(json_path, question, conversation_history, target_pattern):
        seen.update(json_path=json_path, question=question,
                    conversation_history=conversation_history,
                    target_pattern=target_pattern)
        return _PAYLOAD

    kb.set_knowledge_base_client(answer_question)
    ctx = _ctx(episodic=[{"turn_id": "x", "question": "What is its population?",
                          "final_answer": "Approximately 2.1 million."}])
    out = _invoke("What is its population?",
                  "Persistent revolving balance leading to default", ctx=ctx)

    assert out["status"] == "ok"
    assert seen["json_path"] == "/fake/aggregated.json"
    assert seen["question"] == "What is its population?"
    assert seen["target_pattern"] == "Persistent revolving balance leading to default"
    assert seen["conversation_history"] == [
        {"turn_id": "1", "question": "What is its population?",
         "answer": "Approximately 2.1 million."}]
    # The fold-into-question fallback must NOT fire when the slot exists.
    assert "Pattern to retrieve:" not in seen["question"]


def test_search_text_is_surfaced_when_it_differs_from_retrieval_query():
    """`search_text` is retrieval_query blended with our target_pattern — what
    was ACTUALLY searched. It tells the specialist whether a weak result came
    from a weak pattern, which is what its re-ask decision turns on."""
    kb.set_knowledge_base_client(lambda **kw: _PAYLOAD)
    out = _invoke("q", "p")
    assert out["search_text"].startswith("persistent revolving balance")
    assert "balance near limit" in out["search_text"]


def test_search_text_is_omitted_when_it_repeats_the_retrieval_query():
    payload = {**_PAYLOAD, "search_text": _PAYLOAD["retrieval_query"]}
    kb.set_knowledge_base_client(lambda **kw: payload)
    assert "search_text" not in _invoke("q", "p")


def test_missing_search_text_is_not_an_error():
    payload = {k: v for k, v in _PAYLOAD.items() if k != "search_text"}
    kb.set_knowledge_base_client(lambda **kw: payload)
    out = _invoke("q", "p")
    assert out["status"] == "ok"
    assert "search_text" not in out


def test_every_documented_bullet_and_cluster_field_is_read_or_deliberately_dropped():
    """Guards against a silent field rename on the platform side: the fields we
    map must all appear, and the ones we drop are dropped on purpose (the
    per-cluster sub-scores and cluster_key are retrieval bookkeeping)."""
    kb.set_knowledge_base_client(lambda **kw: _PAYLOAD)
    out = _invoke("q", "p")

    pattern = out["patterns"][0]
    assert pattern["pattern"] == "Balance near limit for 6+ months before default"
    assert pattern["score"] == 0.786          # final_score, not cluster_similarity
    assert "cluster_key" not in pattern and "bullet_cluster_score" not in pattern

    bullet = out["evidence"][0]
    assert bullet["point"] == "Balance held at 95% of limit"        # <- text
    assert bullet["why"] == "Signals no repayment capacity"         # <- rationale
    assert bullet["quote"] == "The account remained at 95% …"       # <- raw_quote
    assert bullet["similarity"] == 0.79
    assert bullet["pattern_type"] == "common"


# ── the simulated client, wired exactly as the real one will be ─────────────
#
# `tests/doubles/knowledge_base_sim.py` is what makes the tool testable end-to-end
# before the platform's script lands. These tests keep the two from drifting:
# if the simulation stops matching the documented contract it stops being a
# rehearsal, and a green suite would mean nothing.

_SIM = "tests.doubles.knowledge_base_sim:answer_question"


@pytest.fixture
def sim(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BASE_CLIENT", _SIM)
    for knob in ("KB_SIM_FAIL", "KB_SIM_EMPTY", "KB_SIM_DELAY_S"):
        monkeypatch.delenv(knob, raising=False)
    kb.set_knowledge_base_client(None)


def test_sim_declares_the_documented_signature():
    import inspect as _inspect

    from tests.doubles import knowledge_base_sim

    params = list(_inspect.signature(knowledge_base_sim.answer_question).parameters)
    assert params == ["json_path", "question", "conversation_history",
                      "target_pattern"]
    # Sync, so the tool exercises its `asyncio.to_thread` path as it will in prod.
    assert not _inspect.iscoroutinefunction(knowledge_base_sim.answer_question)


def test_sim_answers_through_the_tool(sim):
    out = _invoke("any other similar cases like this one?",
                  "revolving balance near limit for 6+ months with "
                  "minimum-due-only payments, ending in default")

    assert out["status"] == "ok"
    assert out["similar_cases"], "the simulation should match its own corpus"
    assert all(c.startswith("sim_case_") for c in out["similar_cases"])
    top = out["patterns"][0]
    assert top["pattern_type"] in {"common", "unique"}
    assert top["cases"] and top["score"] > 0
    assert out["evidence"][0]["quote"]


def test_sim_resolves_a_subjectless_follow_up_from_history(sim):
    """"any others like that?" carries no pattern of its own — the KB has to
    resolve it against the conversation, which is why history is in the
    contract at all."""
    ctx = _ctx(episodic=[{
        "turn_id": "abc",
        "question": "Why did the revolving balance climb through 2024?",
        "final_answer": "Utilisation rose from 40% to 95% while payments "
                        "stayed at the minimum due.",
    }])
    out = _invoke("any others like that?", "minimum-due-only payments", ctx=ctx)

    assert out["status"] == "ok"
    assert "revolving balance" in out["retrieval_query"].lower()
    assert out["similar_cases"]


def test_sim_empty_knob_is_a_clean_no_match(sim, monkeypatch):
    monkeypatch.setenv("KB_SIM_EMPTY", "1")
    out = _invoke("q", "a pattern nothing matches")
    assert out["status"] == "ok"
    assert out["similar_cases"] == []
    assert "matched no prior case" in out["note"]


def test_sim_fail_knob_degrades_to_unavailable(sim, monkeypatch):
    monkeypatch.setenv("KB_SIM_FAIL", "1")
    out = _invoke("q", "p")
    assert out["status"] == "unavailable"
    assert "RuntimeError" in out["note"]


def test_sim_delay_knob_trips_the_timeout(sim, monkeypatch):
    monkeypatch.setenv("KB_SIM_DELAY_S", "2")
    monkeypatch.setattr(kb, "_TIMEOUT_S", 0.2)
    out = _invoke("q", "p")
    assert out["status"] == "unavailable"
    assert "did not respond" in out["note"]


# ── the master switch ───────────────────────────────────────────────────────
#
# OFF and BROKEN must not look alike to a reviewer. Off is a complete answer
# ("not applicable"); broken is a fault the deployment should see.

def test_switched_off_answers_not_applicable(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BASE_ENABLED", "false")
    kb.set_knowledge_base_client(lambda **kw: _PAYLOAD)   # present but unused
    out = _invoke("any other similar cases?", "some pattern")

    assert out["status"] == "disabled"
    assert out["answer"] == "not applicable"
    assert "not applicable" in out["note"]
    assert "similar_cases" not in out


def test_switched_off_never_reaches_the_client(monkeypatch):
    """Off means off: no import, no retrieval, no latency, no partial result."""
    monkeypatch.setenv("KNOWLEDGE_BASE_ENABLED", "0")
    calls = []
    kb.set_knowledge_base_client(lambda **kw: calls.append(kw) or _PAYLOAD)
    _invoke("q", "p")
    assert calls == []


def test_switched_off_beats_an_empty_call(monkeypatch):
    """Phrasing cannot change the answer when the feature is off — the switch
    is checked before the empty-argument guard."""
    monkeypatch.setenv("KNOWLEDGE_BASE_ENABLED", "0")
    assert _invoke("  ", "  ")["status"] == "disabled"


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", ""])
def test_falsey_spellings_all_mean_off(monkeypatch, value):
    """`config/tuning.yaml` writes a Python bool through str(), so "False" with
    a capital F reaches the env — it must not read as truthy."""
    monkeypatch.setenv("KNOWLEDGE_BASE_ENABLED", value)
    assert kb.is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_truthy_spellings_all_mean_on(monkeypatch, value):
    monkeypatch.setenv("KNOWLEDGE_BASE_ENABLED", value)
    assert kb.is_enabled() is True


def test_auto_is_off_when_nothing_is_wired(monkeypatch):
    """A dev box with no KB reads as OFF, not as broken — which is why the
    reviewer gets "not applicable" rather than an error narrative."""
    monkeypatch.delenv("KNOWLEDGE_BASE_ENABLED", raising=False)
    monkeypatch.delenv("KNOWLEDGE_BASE_CLIENT", raising=False)
    kb.set_knowledge_base_client(None)
    assert kb.is_enabled() is False
    assert _invoke("q", "p")["status"] == "disabled"


def test_auto_is_on_once_client_and_json_path_are_set(monkeypatch):
    """Setting the two variables is enough — no third switch to remember."""
    monkeypatch.delenv("KNOWLEDGE_BASE_ENABLED", raising=False)
    monkeypatch.setenv("KNOWLEDGE_BASE_CLIENT", _SIM)
    monkeypatch.setenv("KNOWLEDGE_BASE_JSON", "/fake/aggregated.json")
    kb.set_knowledge_base_client(None)
    assert kb.is_enabled() is True
    assert _invoke("any similar cases?", "revolving balance near limit")["status"] == "ok"


def test_auto_is_on_for_an_injected_client(monkeypatch):
    """`set_knowledge_base_client` is a deliberate act — it turns the feature on
    the same way configuring one does, so tests don't need the switch too."""
    monkeypatch.delenv("KNOWLEDGE_BASE_ENABLED", raising=False)
    kb.set_knowledge_base_client(lambda **kw: _PAYLOAD)
    assert kb.is_enabled() is True


def test_the_switch_is_wired_to_the_tuning_yaml():
    """The knobs must be reachable from config/tuning.yaml, not just the env."""
    from config.tuning_loader import _MAP

    assert _MAP["knowledge_base.enabled"] == "KNOWLEDGE_BASE_ENABLED"
    for dotted, env_name in {
        "knowledge_base.client": "KNOWLEDGE_BASE_CLIENT",
        "knowledge_base.json_path": "KNOWLEDGE_BASE_JSON",
        "knowledge_base.timeout_s": "KNOWLEDGE_BASE_TIMEOUT_S",
        "knowledge_base.max_clusters": "KNOWLEDGE_BASE_MAX_CLUSTERS",
        "knowledge_base.max_bullets": "KNOWLEDGE_BASE_MAX_BULLETS",
        "knowledge_base.text_chars": "KNOWLEDGE_BASE_TEXT_CHARS",
        "knowledge_base.history_turns": "KNOWLEDGE_BASE_HISTORY_TURNS",
    }.items():
        assert _MAP[dotted] == env_name


# ── against a REAL payload ──────────────────────────────────────────────────
#
# `fixtures/knowledge_base_real_payload.json` is an actual response from the
# platform's script. The mock above is convenient; this is what the field
# lengths, the score distribution and the case_id format really look like, and
# three of the decisions below were made FROM it rather than guessed.

_FIXTURE = (pathlib.Path(__file__).parent / "fixtures"
            / "knowledge_base_real_payload.json")


@pytest.fixture
def real_payload():
    return json.loads(_FIXTURE.read_text())


def test_real_payload_compacts_to_a_sane_context_cost(real_payload):
    """~15KB raw. The caps exist because this lands in a specialist's context
    on top of an already-large system prompt; if a change doubles it, that
    should be a decision, not a surprise."""
    raw = len(json.dumps(real_payload))
    out = json.dumps(summarize_kb_result(real_payload))
    assert raw > 15_000
    assert len(out) < 11_000, f"compaction regressed: {len(out)} chars"


def test_real_answer_is_not_cut_mid_argument(real_payload):
    """The KB's synthesis runs ~2100 chars and is the most useful field. The
    old shared 1200-char cap severed it inside its own numbered list."""
    out = summarize_kb_result(real_payload)
    assert len(out["answer"]) >= 1500


def test_evidence_is_taken_round_robin_across_clusters(real_payload, monkeypatch):
    """Real scores were 0.594 / 0.591 / 0.580 — packed tight enough that head
    truncation is near-arbitrary, and the cluster that actually answered the
    question ranked LAST. Every cluster must survive a tight cap."""
    monkeypatch.setattr(kb, "_MAX_BULLETS", 3)
    out = summarize_kb_result(real_payload)

    kept = {e["case_id"] for e in out["evidence"]}
    assert len(kept) == 3
    # One from each of the three clusters, not three from the top one.
    by_case = {b["case_id"]: b["cluster_key"]
               for b in real_payload["relevant_bullets"]}
    assert len({by_case[c] for c in kept}) == 3


def test_every_matched_case_survives_a_tight_cap(real_payload, monkeypatch):
    """The caps trim supporting detail, never the answer to "which cases"."""
    monkeypatch.setattr(kb, "_MAX_BULLETS", 2)
    out = summarize_kb_result(real_payload)
    assert len(out["similar_cases"]) == 9
    assert out["evidence_omitted"] == 7


def test_the_case_under_review_is_not_returned_as_its_own_precedent(real_payload):
    """Case ids here are bare digit strings — identical in shape to ours — and
    this case's own report may well be in the corpus the KB was built from.
    Citing it back to the reviewer as a similar case is actively misleading."""
    out = summarize_kb_result(real_payload, self_case_id="613555467013")

    assert "613555467013" not in out["similar_cases"]
    assert all(e["case_id"] != "613555467013" for e in out["evidence"])
    assert out["excluded_self_case"] == "613555467013"
    assert len(out["similar_cases"]) == 8      # the other eight are untouched


def test_self_filtering_is_silent_when_the_case_is_absent(real_payload):
    out = summarize_kb_result(real_payload, self_case_id="999999999999")
    assert "excluded_self_case" not in out
    assert len(out["similar_cases"]) == 9


def test_the_tool_passes_the_case_under_review_through(real_payload):
    """The filter is only as good as its wiring: `_case_id` lives on AppContext
    and must actually reach `summarize_kb_result`."""
    kb.set_knowledge_base_client(lambda **kw: real_payload)
    app_ctx = types.SimpleNamespace(_episodic_records=[], logger=None,
                                    _case_id="613555467013")
    out = json.loads(asyncio.run(kb.knowledge_base_search.on_invoke_tool(
        types.SimpleNamespace(context=app_ctx),
        json.dumps({"question": "any similar cases?", "target_pattern": "p"}))))

    assert out["excluded_self_case"] == "613555467013"
    assert "613555467013" not in out["similar_cases"]


def test_rationale_is_trimmed_harder_than_the_quote(real_payload):
    """`rationale` averages ~340 chars of commentary on the KB's OWN selection.
    The quote is what a specialist can cite, so it keeps the full budget."""
    out = summarize_kb_result(real_payload)
    assert all(len(e.get("why", "")) <= (kb._MAX_TEXT_CHARS // 2) + 1
               for e in out["evidence"])
    assert any(len(e.get("quote", "")) > (kb._MAX_TEXT_CHARS // 2) + 1
               for e in out["evidence"])


def test_real_search_text_survives_and_carries_the_target_pattern(real_payload):
    """In the real payload `retrieval_query` is a generic rewrite that LOSES the
    pattern; only `search_text` shows it was searched on."""
    out = summarize_kb_result(real_payload)
    assert "revolving balance" not in out["retrieval_query"].lower()
    assert "revolving balance" in out["search_text"].lower()
