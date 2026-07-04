# Episodic Context Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject recent turns' structured `question → sub-answers → final-answer` records (built from `qa_cache`) into orchestrator + specialist context so follow-ups can resolve coreference ("when did *it* reach the *second* spike?").

**Architecture:** New pure module `tools/episodic.py` parses `qa_cache` entries into records and selects/renders them; `qa_cache` entries gain a monotonic `turn_seq` for true turn-recency (immune to LRU reordering); `server.py` threads a bounded record window into `AppContext` and prepends the orchestrator block; `tools/redacting_tool.py` prepends each specialist's own-history slice. Purely additive.

**Tech Stack:** Python 3.11, pytest, the `autoAI` pyenv virtualenv.

## Global Constraints

- **Interpreter:** always `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python` (never bare `python` — base 3.11.13 lacks matplotlib; see `.claude/memory/dev_env_autoai_interpreter.md`). Never `pip install` / change deps.
- **Additive, guarded:** episodic assembly must never raise into the turn — wrap injection in try/except; degrade to no-block. Empty/first-turn `qa_cache` → no block (behavior identical to today).
- **Env-tunable knobs (exact defaults):** `EPISODIC_TURNS=3`, `EPISODIC_WINDOW=10`, `EPISODIC_ANSWER_CHARS=800`, `EPISODIC_SUBANSWER_CHARS=400`.
- **`sub_answer`** = `findings` (SpecialistOutput) **else** `answer` (report_agent ReportDraft); `report_agent` IS included.
- **Gate:** the existing suite must stay green and the new tests pass — run `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q` (current baseline on this branch = 461 passed; count rises as new tests land — no test may go from pass→fail).
- **Base branch:** cut from `refactor/redacting-tool-decomposition` (it has `tools/redacting_tool.py` + a clean `server.py`); reconcile with PR #18's `server.py` at merge.

---

## File Structure

- `tools/episodic.py` (Create) — pure: parse payload → sub-answer, build records from `qa_cache`, `select_episodic` / `select_specialist_episodic`, render blocks. No I/O, no LLM.
- `server.py` (Modify) — `turn_seq` counter on `CaseSession` + stamp in `_store_cached_qa`; build the record window, thread into `AppContext`, prepend the orchestrator block.
- `agent_factories/app_context.py` (Modify) — `_episodic_records` field.
- `tools/redacting_tool.py` (Modify) — prepend the specialist slice in `_runner`.
- Tests under `tests/test_tools/`.

---

## Task 1: `tools/episodic.py` — parse / select / render

**Files:**
- Create: `tools/episodic.py`
- Test: `tests/test_tools/test_episodic.py`

**Interfaces:**
- Produces: `_parse_sub_answer(payload) -> str | None`; `build_records(qa_cache: dict, window: int = EPISODIC_WINDOW) -> list[dict]` (records newest-first, each `{turn_id, question, sub_answers:[{specialist, sub_question, sub_answer}], final_answer}`); `select_episodic(records, k=EPISODIC_TURNS) -> list[dict]`; `select_specialist_episodic(records, specialist, k=EPISODIC_TURNS) -> list[dict]` (`[{sub_question, sub_answer}]`); `render_orchestrator_block(records) -> str`; `render_specialist_block(pairs) -> str`; module constants `EPISODIC_TURNS/WINDOW/ANSWER_CHARS/SUBANSWER_CHARS`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools/test_episodic.py
from tools.episodic import (
    _parse_sub_answer, build_records, select_episodic,
    select_specialist_episodic, render_orchestrator_block, render_specialist_block,
)


def _entry(seq, q, calls, answer="A"):
    return {"turn_seq": seq, "turn_id_origin": f"t{seq}", "origin_question": q,
            "answer": answer, "tool_calls": calls}


def _call(tool, subq, payload):
    return {"call_id": f"c-{tool}", "tool": tool, "sub_question": subq, "payload": payload}


def test_parse_sub_answer_findings_and_prefix():
    p = '[Sub-question: x]\n{"domain":"modeling","findings":"TSR 39.6","evidence":[1]}'
    assert _parse_sub_answer(p) == "TSR 39.6"


def test_parse_sub_answer_falls_back_to_answer_for_report_agent():
    p = '{"coverage":"implicit","answer":"elevated external delinquency"}'
    assert _parse_sub_answer(p) == "elevated external delinquency"


def test_parse_sub_answer_skips_non_json_and_failed():
    assert _parse_sub_answer("[FAILED modeling] timeout: ...") is None
    assert _parse_sub_answer("just prose") is None
    assert _parse_sub_answer('{"domain":"x"}') is None          # neither field
    assert _parse_sub_answer(None) is None


def test_parse_sub_answer_truncates(monkeypatch):
    import tools.episodic as ep
    monkeypatch.setattr(ep, "EPISODIC_SUBANSWER_CHARS", 5)
    assert ep._parse_sub_answer('{"findings":"abcdefgh"}') == "abcde"


def test_build_records_orders_by_turn_seq_desc_not_dict_order():
    qa = {  # dict insertion order is 1,2,3 but turn_seq says 3 is newest
        "q1": _entry(1, "Q1", [_call("modeling", "sq1", '{"findings":"f1"}')]),
        "q3": _entry(3, "Q3", [_call("modeling", "sq3", '{"findings":"f3"}')]),
        "q2": _entry(2, "Q2", [_call("spend_payments", "sq2", '{"findings":"f2"}')]),
    }
    recs = build_records(qa)
    assert [r["question"] for r in recs] == ["Q3", "Q2", "Q1"]
    assert recs[0]["sub_answers"][0] == {
        "specialist": "modeling", "sub_question": "sq3", "sub_answer": "f3"}


def test_build_records_final_answer_truncates(monkeypatch):
    import tools.episodic as ep
    monkeypatch.setattr(ep, "EPISODIC_ANSWER_CHARS", 4)
    recs = ep.build_records({"q": _entry(1, "Q", [], answer="abcdefg")})
    assert recs[0]["final_answer"] == "abcd"


def test_build_records_window_and_bad_subanswers():
    qa = {f"q{i}": _entry(i, f"Q{i}",
          [_call("modeling", "s", '{"findings":"ok"}'),
           _call("report_agent", "s", "not-json")]) for i in range(1, 16)}
    recs = build_records(qa, window=10)
    assert len(recs) == 10                       # window bound
    assert [sa["specialist"] for sa in recs[0]["sub_answers"]] == ["modeling"]  # bad one skipped
    assert recs[0]["question"] == "Q15"          # newest


def test_select_episodic_takes_first_k():
    recs = [{"question": f"Q{i}"} for i in range(5)]
    assert [r["question"] for r in select_episodic(recs, k=3)] == ["Q0", "Q1", "Q2"]


def test_select_specialist_episodic_own_history_reaches_back():
    # modeling ran in the OLDEST turn only; still returned (not empty).
    recs = [
        {"sub_answers": [{"specialist": "spend_payments", "sub_question": "a", "sub_answer": "sa"}]},
        {"sub_answers": [{"specialist": "spend_payments", "sub_question": "b", "sub_answer": "sb"}]},
        {"sub_answers": [{"specialist": "modeling", "sub_question": "c", "sub_answer": "sc"}]},
    ]
    out = select_specialist_episodic(recs, "modeling", k=3)
    assert out == [{"sub_question": "c", "sub_answer": "sc"}]
    assert select_specialist_episodic(recs, "bureau", k=3) == []


def test_render_blocks_empty_and_nonempty():
    assert render_orchestrator_block([]) == ""
    assert render_specialist_block([]) == ""
    b = render_orchestrator_block([{"question": "How did CDSS react?",
        "sub_answers": [{"specialist": "modeling", "sub_question": "x",
                         "sub_answer": "CDSS spiked 2024-06 and 2024-11"}],
        "final_answer": "CDSS rose..."}])
    assert "EPISODIC" in b and "CDSS" in b and "2024-11" in b
    s = render_specialist_block([{"sub_question": "x", "sub_answer": "CDSS spiked 2024-11"}])
    assert "EPISODIC" in s and "2024-11" in s
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'tools.episodic'`).

- [ ] **Step 3: Implement `tools/episodic.py`**

```python
"""Episodic conversation tier: parse qa_cache turns into structured
question→sub-answers→final-answer records, and select/render them for injection
into orchestrator + specialist context (coreference / continuity). Source is
qa_cache. See docs/superpowers/specs/2026-07-05-episodic-context-injection-design.md.
Pure — no I/O, no LLM."""
from __future__ import annotations

import json
import os

EPISODIC_TURNS = int(os.environ.get("EPISODIC_TURNS", "3"))
EPISODIC_WINDOW = int(os.environ.get("EPISODIC_WINDOW", "10"))
EPISODIC_ANSWER_CHARS = int(os.environ.get("EPISODIC_ANSWER_CHARS", "800"))
EPISODIC_SUBANSWER_CHARS = int(os.environ.get("EPISODIC_SUBANSWER_CHARS", "400"))

_SUBQ_PREFIX = "[Sub-question:"


def _parse_sub_answer(payload) -> str | None:
    """Extract a concise sub-answer from a stored tool_call payload.
    SpecialistOutput → `findings`; report_agent ReportDraft → `answer`.
    Returns None (skip) on non-JSON / [FAILED …] / neither field."""
    if not isinstance(payload, str) or not payload:
        return None
    text = payload
    if text.startswith(_SUBQ_PREFIX):            # strip "[Sub-question: ...]\n"
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    val = obj.get("findings")
    if not isinstance(val, str) or not val:
        val = obj.get("answer")
    if not isinstance(val, str) or not val:
        return None
    return val[:EPISODIC_SUBANSWER_CHARS]


def build_records(qa_cache: dict, window: int = EPISODIC_WINDOW) -> list[dict]:
    """qa_cache entries → episodic records, newest-first by turn_seq, bounded to
    `window`. Entries missing turn_seq sort oldest."""
    if not isinstance(qa_cache, dict) or not qa_cache:
        return []
    entries = sorted(qa_cache.values(),
                     key=lambda e: e.get("turn_seq", -1), reverse=True)[:window]
    records: list[dict] = []
    for e in entries:
        sub_answers = []
        for tc in e.get("tool_calls") or []:
            sa = _parse_sub_answer(tc.get("payload"))
            if sa is None:
                continue
            sub_answers.append({"specialist": tc.get("tool"),
                                "sub_question": tc.get("sub_question"),
                                "sub_answer": sa})
        records.append({"turn_id": e.get("turn_id_origin"),
                        "question": e.get("origin_question"),
                        "sub_answers": sub_answers,
                        "final_answer": (e.get("answer") or "")[:EPISODIC_ANSWER_CHARS]})
    return records


def select_episodic(records: list[dict], k: int = EPISODIC_TURNS) -> list[dict]:
    """The newest k whole records (records already newest-first)."""
    return records[:k]


def select_specialist_episodic(records: list[dict], specialist: str,
                               k: int = EPISODIC_TURNS) -> list[dict]:
    """This specialist's OWN newest k {sub_question, sub_answer} across the window
    (filter-then-take-k), so it still sees its answers even if it didn't run in
    the global recent turns."""
    out: list[dict] = []
    for rec in records:                          # newest-first
        for sa in rec.get("sub_answers") or []:
            if sa.get("specialist") == specialist:
                out.append({"sub_question": sa.get("sub_question"),
                            "sub_answer": sa.get("sub_answer")})
                if len(out) >= k:
                    return out
    return out


def render_orchestrator_block(records: list[dict]) -> str:
    if not records:
        return ""
    return ('[EPISODIC — recent turns this session, newest first. Use to resolve '
            'references ("it", "the second spike") and to avoid re-asking:\n'
            + json.dumps(records, ensure_ascii=False, default=str) + "\n]")


def render_specialist_block(pairs: list[dict]) -> str:
    if not pairs:
        return ""
    return ("[EPISODIC — your own recent answers this session, newest first:\n"
            + json.dumps(pairs, ensure_ascii=False, default=str) + "\n]")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add tools/episodic.py tests/test_tools/test_episodic.py
git commit -m "feat(episodic): parse qa_cache into records + selectors + renderers"
```

---

## Task 2: `turn_seq` stamp on `qa_cache` entries

**Files:**
- Modify: `server.py` (`CaseSession` dataclass; `_store_cached_qa`)
- Test: `tests/test_tools/test_episodic_turn_seq.py`

**Interfaces:**
- Consumes: nothing.
- Produces: every `qa_cache` entry stored via `_store_cached_qa` carries `turn_seq: int`, strictly increasing per session (so `build_records` orders by true turn recency).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_episodic_turn_seq.py
import server


def _mk_session():
    # Minimal CaseSession-like object for _store_cached_qa (which only touches
    # sess.qa_cache and the turn_seq counter). Use the real class if constructible;
    # else a SimpleNamespace with the needed attrs.
    import types
    return types.SimpleNamespace(qa_cache={}, _qa_turn_seq=0)


def test_store_stamps_increasing_turn_seq():
    sess = _mk_session()
    server._store_cached_qa(sess, "q1", {"answer": "a1"})
    server._store_cached_qa(sess, "q2", {"answer": "a2"})
    seqs = [sess.qa_cache["q1"]["turn_seq"], sess.qa_cache["q2"]["turn_seq"]]
    assert seqs[0] < seqs[1]                      # strictly increasing
```

- [ ] **Step 2: Run to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic_turn_seq.py -q`
Expected: FAIL (`KeyError: 'turn_seq'`).

- [ ] **Step 3: Implement**

In `server.py` `CaseSession` dataclass, add a counter field near `qa_cache`:
```python
    _qa_turn_seq: int = 0   # monotonic per-session sequence for episodic ordering
```
In `_store_cached_qa`, stamp the value before storing (locate the `sess.qa_cache[cache_key] = value` line):
```python
def _store_cached_qa(sess, cache_key, value):
    if not cache_key:
        return 0
    sess._qa_turn_seq += 1
    value["turn_seq"] = sess._qa_turn_seq
    sess.qa_cache[cache_key] = value
    ...  # (existing eviction loop unchanged)
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic_turn_seq.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite (no regression)**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: 461 passed + the 2 new files' tests (no pass→fail).

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_tools/test_episodic_turn_seq.py
git commit -m "feat(episodic): stamp monotonic turn_seq on qa_cache entries"
```

---

## Task 3: AppContext field + orchestrator injection

**Files:**
- Modify: `agent_factories/app_context.py`
- Modify: `server.py` (orchestrator input framing)
- Test: `tests/test_tools/test_episodic_framing.py`

**Interfaces:**
- Consumes: `tools.episodic.build_records`, `select_episodic`, `render_orchestrator_block`, `EPISODIC_TURNS` (Task 1); `qa_cache` with `turn_seq` (Task 2).
- Produces: `AppContext._episodic_records: list` (the window, threaded to specialists in Task 4); `server._compose_framed_question(episodic_block, warmth_hint, question) -> str` (joins non-empty parts with `\n\n`, order: episodic, warmth, question).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_episodic_framing.py
import server


def test_compose_framed_question_orders_and_skips_empty():
    out = server._compose_framed_question("[EPISODIC ...]", "[KB-warmth ...]", "Q?")
    assert out == "[EPISODIC ...]\n\n[KB-warmth ...]\n\nQ?"
    # empty episodic + empty warmth → just the question
    assert server._compose_framed_question("", "", "Q?") == "Q?"
    # episodic present, warmth empty
    assert server._compose_framed_question("[EP]", "", "Q?") == "[EP]\n\nQ?"
```

- [ ] **Step 2: Run to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic_framing.py -q`
Expected: FAIL (`_compose_framed_question` not defined).

- [ ] **Step 3: Implement**

`agent_factories/app_context.py` — add the field to the `AppContext` dataclass:
```python
    # Episodic record window (built from qa_cache each turn) — this specialist's
    # own slice is prepended to its sub-question by redacting_tool._runner.
    _episodic_records: list = field(default_factory=list)
```

`server.py` — add the composer near the other framing helpers:
```python
def _compose_framed_question(episodic_block: str, warmth_hint: str, question: str) -> str:
    """Order: episodic (coreference) → KB warmth (topics) → question. Skip empties."""
    return "\n\n".join(p for p in (episodic_block, warmth_hint, question) if p)
```

Then wire it where `framed_question` is currently built alongside `warmth_hint` (locate `warmth_hint = _format_kb_warmth_hint(...)` and the `framed_question = ...` that follows). Replace the `framed_question` assignment with:
```python
    from tools.episodic import (build_records, select_episodic,
                                render_orchestrator_block, EPISODIC_TURNS)
    try:
        episodic_window = build_records(sess.qa_cache)
        episodic_block = render_orchestrator_block(
            select_episodic(episodic_window, EPISODIC_TURNS))
    except Exception:  # noqa: BLE001 — episodic assembly must never break a turn
        episodic_window, episodic_block = [], ""
    framed_question = _compose_framed_question(
        episodic_block, warmth_hint, verdict.redacted_question)
```
And where the per-turn `AppContext(...)` is constructed (the `ctx = AppContext(...)` call), pass the window through:
```python
        _episodic_records=episodic_window,
```
(If `ctx` is built before `episodic_window` exists, move the `build_records` call above the `ctx = AppContext(...)` line, or set `ctx._episodic_records = episodic_window` right after construction.)

- [ ] **Step 4: Run to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic_framing.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: no pass→fail (461 + new tests).

- [ ] **Step 6: Commit**

```bash
git add agent_factories/app_context.py server.py tests/test_tools/test_episodic_framing.py
git commit -m "feat(episodic): thread record window into AppContext + orchestrator block"
```

---

## Task 4: Specialist slice injection in `redacting_tool._runner`

**Files:**
- Modify: `tools/redacting_tool.py` (`_runner`, `contextual_in` construction)
- Test: `tests/test_tools/test_episodic_specialist_slice.py`

**Interfaces:**
- Consumes: `tools.episodic.select_specialist_episodic`, `render_specialist_block`, `EPISODIC_TURNS` (Task 1); `AppContext._episodic_records` (Task 3).
- Produces: `tools.redacting_tool._compose_specialist_input(episodic_block, kb_digest, sub_question) -> str` (joins non-empty with `\n\n`, order: episodic, kb_digest, sub_question) — used inside `_runner`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools/test_episodic_specialist_slice.py
import types
from tools.redacting_tool import _compose_specialist_input
from tools.episodic import select_specialist_episodic, render_specialist_block


def test_compose_specialist_input_order_and_skips():
    out = _compose_specialist_input("[EP mine]", "[KB digest]", "sub-q?")
    assert out == "[EP mine]\n\n[KB digest]\n\nsub-q?"
    assert _compose_specialist_input("", "", "sub-q?") == "sub-q?"


def test_specialist_slice_is_own_history_only():
    # Records where 'modeling' answered earlier; building modeling's block yields
    # only modeling's pair, not spend_payments'.
    records = [
        {"sub_answers": [{"specialist": "spend_payments", "sub_question": "a", "sub_answer": "sa"}]},
        {"sub_answers": [{"specialist": "modeling", "sub_question": "c", "sub_answer": "CDSS 2024-11"}]},
    ]
    block = render_specialist_block(select_specialist_episodic(records, "modeling", 3))
    assert "CDSS 2024-11" in block and "spend_payments" not in block
```

- [ ] **Step 2: Run to verify it fails**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic_specialist_slice.py -q`
Expected: FAIL (`_compose_specialist_input` not defined).

- [ ] **Step 3: Implement**

In `tools/redacting_tool.py`, add the composer at module level:
```python
def _compose_specialist_input(episodic_block: str, kb_digest: str, sub_question: str) -> str:
    """Order: this specialist's episodic slice → KB digest → the sub-question."""
    return "\n\n".join(p for p in (episodic_block, kb_digest, sub_question) if p)
```
In `_runner`, on the first-call branch where the KB digest is currently prepended (locate where `contextual_in` is built from `kb_digest` + `redacted_in`), compute the specialist's slice and route through the composer:
```python
        from tools.episodic import (select_specialist_episodic,
                                    render_specialist_block, EPISODIC_TURNS)
        try:
            _recs = getattr(app_ctx, "_episodic_records", None) or []
            episodic_block = render_specialist_block(
                select_specialist_episodic(_recs, name, EPISODIC_TURNS))
        except Exception:  # noqa: BLE001 — never break the specialist call
            episodic_block = ""
        contextual_in = _compose_specialist_input(
            episodic_block, kb_digest, redacted_in)
```
Keep the existing guard: this only applies on the first call of the turn (`not prior`), same as the KB digest — on within-turn follow-ups `prior` already carries it.

- [ ] **Step 4: Run to verify it passes**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic_specialist_slice.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: no pass→fail.

- [ ] **Step 6: Commit**

```bash
git add tools/redacting_tool.py tests/test_tools/test_episodic_specialist_slice.py
git commit -m "feat(episodic): prepend specialist's own recent-answers slice"
```

---

## Task 5: Integration — the coreference scenario

**Files:**
- Test: `tests/test_tools/test_episodic_integration.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the test**

```python
# tests/test_tools/test_episodic_integration.py
from tools.episodic import (build_records, select_episodic, render_orchestrator_block,
                            select_specialist_episodic, render_specialist_block)


def test_cdss_followup_has_prior_context():
    # Prior turn: "How did CDSS react?" answered by modeling with two spikes.
    qa_cache = {
        "how did cdss react?": {
            "turn_seq": 1, "turn_id_origin": "t1",
            "origin_question": "How did CDSS react?",
            "answer": "CDSS rose through 2024, spiking at 2024-06 and 2024-11.",
            "tool_calls": [{"call_id": "c1", "tool": "modeling",
                            "sub_question": "CDSS trajectory + drivers",
                            "payload": '{"domain":"modeling","findings":"CDSS spiked '
                                       'at 2024-06 and 2024-11.","evidence":[1]}'}],
        },
    }
    records = build_records(qa_cache)

    # Orchestrator sees the prior turn → can resolve "it" and "the second spike".
    orch = render_orchestrator_block(select_episodic(records, 3))
    assert "CDSS" in orch and "2024-11" in orch and "How did CDSS react?" in orch

    # modeling, re-invoked, sees its OWN prior CDSS answer.
    mine = render_specialist_block(select_specialist_episodic(records, "modeling", 3))
    assert "2024-11" in mine
```

- [ ] **Step 2: Run**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/test_tools/test_episodic_integration.py -q`
Expected: PASS.

- [ ] **Step 3: Full suite once more**

Run: `~/.pyenv/versions/3.11.13/envs/autoAI/bin/python -m pytest tests/ -q`
Expected: no pass→fail; all new episodic tests green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tools/test_episodic_integration.py
git commit -m "test(episodic): coreference scenario end-to-end"
```

---

## Self-Review

**Spec coverage:**
- §3 record shape → Task 1 `build_records`.
- §4 `turn_seq` → Task 2.
- §5 parser (findings/answer, report_agent, truncation, skip) → Task 1 `_parse_sub_answer`.
- §6 both selectors → Task 1.
- §7a orchestrator injection → Task 3.
- §7b specialist injection (own history) → Task 4.
- §9 bounding → constants (Task 1) + `select_*`.
- §10 edge cases (empty/first-turn/parse-fail/cache-hit) → Task 1 tests + the try/except guards in Tasks 3–4; cache-hit path is untouched (injection only on the fresh path).
- §11 tests → Tasks 1–5.

**Placeholder scan:** none — all code steps carry complete code; the two "locate the line" wiring points (Tasks 3–4) name the exact anchor (`warmth_hint = _format_kb_warmth_hint`, and the `kb_digest`+`redacted_in` `contextual_in` build) and give the full replacement.

**Type consistency:** `build_records → list[dict]` (records with `sub_answers:[{specialist, sub_question, sub_answer}]`) is consumed identically by `select_episodic` (whole records) and `select_specialist_episodic` (emits `{sub_question, sub_answer}`); `render_orchestrator_block(records)` / `render_specialist_block(pairs)` match those shapes; `_compose_framed_question` / `_compose_specialist_input` both take `(block, mid, tail)` and join non-empty with `\n\n`. Consistent across tasks.
