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
