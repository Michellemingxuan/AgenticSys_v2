"""SIMULATED knowledge-base client — a stand-in for the platform's
``answer_question`` so ``tools/knowledge_base.py`` can be exercised
end-to-end in dev.

A TEST DOUBLE, not a tool: no agent can call it, and it lives here rather
than beside `tools/knowledge_base.py` so that nobody reading the tool surface
has to work out which of two knowledge_base modules the specialists reach.

Point the tool at it exactly as you would at the real thing:

    KNOWLEDGE_BASE_CLIENT=tests.doubles.knowledge_base_sim:answer_question
    KNOWLEDGE_BASE_JSON=/any/path/aggregated_rank_top_common_unique.json

(the file-path form works too, which is the more realistic rehearsal:
``KNOWLEDGE_BASE_CLIENT=/abs/path/tests/doubles/knowledge_base_sim.py:answer_question``)

It matches the real contract on every axis that the caller can observe:

* the same four parameters, in the same names, **synchronous** — so it goes
  through `asyncio.to_thread` like a real script would
* the documented output keys, including ``search_text``
* ``retrieval_query`` resolved against ``conversation_history``, so a follow-up
  that says only "any others like that?" still retrieves — the coreference path
  is testable, not just assumed

What it is NOT: a retrieval system. Scoring is token overlap over a small
built-in corpus, no embeddings and no LLM. Case ids are deliberately prefixed
``sim_case_`` so a simulated referral can never be mistaken for a real one in a
transcript.

Test knobs (env, all off by default):

    KB_SIM_DELAY_S=20     sleep before answering — exercises the tool's timeout
    KB_SIM_FAIL=1         raise — exercises the "unavailable" degradation
    KB_SIM_EMPTY=1        return a well-formed no-match result

Standalone check:

    python tests/doubles/knowledge_base_sim.py "any similar cases?" "revolving balance near limit"
"""

from __future__ import annotations

import math
import os
import re
import time
from typing import Any


# ── corpus ──────────────────────────────────────────────────────────────────
#
# Shaped like the real thing: clusters are the COMMON / UNIQUE characteristics
# that came out of clustering ~10 distilled points per case report, and each
# bullet is one case's contribution to its cluster, with the quote it came from.

_CLUSTERS: list[dict] = [
    {
        "cluster_key": "common:1",
        "pattern_type": "common",
        "cluster_text": ("Revolving balance held near the credit limit for six "
                         "or more months with minimum-due-only payments before "
                         "default"),
        "bullets": [
            {"case_id": "sim_case_0142",
             "text": "Balance sat between 92% and 98% of limit for 8 consecutive months",
             "rationale": "Sustained utilisation at the ceiling with no paydown "
                          "indicates the limit had become working capital",
             "raw_quote": "Utilisation remained above 92% from Feb-2024 through "
                          "Sep-2024 while payments never exceeded the minimum due."},
            {"case_id": "sim_case_0377",
             "text": "Minimum-due-only payments for 11 months, then a missed payment",
             "rationale": "Minimum-due behaviour is the last stable state before "
                          "the first delinquency in this cohort",
             "raw_quote": "Every payment from Mar-2024 to Jan-2025 equalled the "
                          "minimum due to the cent."},
            {"case_id": "sim_case_0918",
             "text": "Utilisation above 90% for 6 months preceding charge-off",
             "rationale": "Confirms the six-month floor the cluster is built on",
             "raw_quote": "The account was at or near its limit for the final "
                          "two quarters."},
        ],
    },
    {
        "cluster_key": "common:2",
        "pattern_type": "common",
        "cluster_text": ("Merchant-concentrated spend spike in the two to three "
                         "months preceding delinquency"),
        "bullets": [
            {"case_id": "sim_case_0142",
             "text": "Spend tripled in the 2 months before the first missed payment, "
                     "68% of it at one merchant",
             "rationale": "The spike is concentrated rather than broad, which "
                          "separates distress from ordinary seasonal spend",
             "raw_quote": "Monthly spend rose from $4.1K to $12.8K, with $8.7K "
                          "at a single merchant."},
            {"case_id": "sim_case_0512",
             "text": "Spend doubled across 3 months, dominated by one merchant category",
             "rationale": "Same shape at a lower magnitude",
             "raw_quote": "Category concentration reached 61% in the quarter "
                          "before delinquency."},
        ],
    },
    {
        "cluster_key": "common:3",
        "pattern_type": "common",
        "cluster_text": ("Internal risk scores deteriorate while the external "
                         "bureau score stays healthy"),
        "bullets": [
            {"case_id": "sim_case_0377",
             "text": "CDSS moved 3 bands worse over 5 months while FICO held above 700",
             "rationale": "The internal model saw behaviour the bureau had not "
                          "yet recorded — a lead-time finding",
             "raw_quote": "CDSS deteriorated from band 2 to band 5 between "
                          "Apr and Sep while FICO moved only 11 points."},
            {"case_id": "sim_case_0733",
             "text": "TSR breached threshold 4 months before any bureau derog appeared",
             "rationale": "Quantifies the lead time between internal and "
                          "external signal",
             "raw_quote": "The first external delinquency was reported in "
                          "Nov-2024; TSR had breached in Jul-2024."},
        ],
    },
    {
        "cluster_key": "common:4",
        "pattern_type": "common",
        "cluster_text": ("Repeated returned payments for insufficient funds in "
                         "the quarter before charge-off"),
        "bullets": [
            {"case_id": "sim_case_0512",
             "text": "5 returned payments in 3 months, all insufficient-funds",
             "rationale": "Return reason is uniform, which points at capacity "
                          "rather than a banking or operational error",
             "raw_quote": "Five settlement attempts were returned R01 between "
                          "Oct and Dec."},
            {"case_id": "sim_case_0918",
             "text": "Return rate rose from 0% to 40% of attempts in one quarter",
             "rationale": "The ratio matters more than the count once attempt "
                          "volume varies",
             "raw_quote": "4 of 10 attempts were returned in the final quarter."},
        ],
    },
    {
        "cluster_key": "unique:1",
        "pattern_type": "unique",
        "cluster_text": ("Single-merchant concentration above 60% of total spend "
                         "sustained for more than a year"),
        "bullets": [
            {"case_id": "sim_case_0142",
             "text": "One merchant took 64% of all spend across 14 months",
             "rationale": "Sustained concentration at this level appears in no "
                          "other case in the corpus",
             "raw_quote": "A single merchant accounted for $148K of $231K total "
                          "spend over the period."},
        ],
    },
    {
        "cluster_key": "unique:2",
        "pattern_type": "unique",
        "cluster_text": ("Spend accelerates AFTER the account is flagged for "
                         "collections review"),
        "bullets": [
            {"case_id": "sim_case_0733",
             "text": "Spend rose 40% in the month following the collections flag",
             "rationale": "Behaviour inverts the expected post-flag contraction, "
                          "which is why the case is singular",
             "raw_quote": "Post-flag monthly spend was $9.2K against a $6.6K "
                          "trailing average."},
        ],
    },
    {
        "cluster_key": "unique:3",
        "pattern_type": "unique",
        "cluster_text": ("Business and consumer scores diverge sharply while the "
                         "entity holds no commercial tradelines"),
        "bullets": [
            {"case_id": "sim_case_0866",
             "text": "SBFE scored low with zero commercial tradelines on file",
             "rationale": "A low score with no trade history to score is an "
                          "artefact, not a risk signal",
             "raw_quote": "sbfe_commercial_tradelines = 0 while sbfe_score "
                          "reported in the bottom decile."},
        ],
    },
]

_STOPWORDS = frozenset("""
a an the and or of to in on for with without at by from as is are was were be
been being this that these those it its any some other others like similar
case cases what which how why when who does do did there their them they
""".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOPWORDS and len(w) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    """Cosine-ish token overlap in 0..1. Enough to rank a small corpus."""
    if not a or not b:
        return 0.0
    return round(len(a & b) / math.sqrt(len(a) * len(b)), 3)


def _resolve_retrieval_query(question: str, conversation_history: Any) -> str:
    """What the KB decides to search for, given the question in context.

    A follow-up that carries no subject of its own ("any others like that?")
    is resolved against the most recent prior turn — the same coreference move
    the real client has to make, and the reason `conversation_history` is part
    of the contract at all.
    """
    question = (question or "").strip()
    if len(_tokens(question)) >= 3:
        return question
    history = conversation_history if isinstance(conversation_history, list) else []
    for turn in reversed(history):                    # newest prior turn first
        if not isinstance(turn, dict):
            continue
        prior = " ".join(str(turn.get(k) or "") for k in ("question", "answer"))
        if _tokens(prior):
            return f"{question} {prior}".strip() if question else prior.strip()
    return question


def answer_question(json_path: str,
                    question: str,
                    conversation_history: list | None = None,
                    target_pattern: str = "") -> dict:
    """Simulated retrieval over the built-in corpus. Same shape as the real one.

    `json_path` is accepted and echoed through the ranking as the real client's
    corpus selector would be, but the simulation never reads it — dev has no
    aggregated JSON to read.
    """
    if os.environ.get("KB_SIM_FAIL"):
        raise RuntimeError("simulated knowledge-base failure (KB_SIM_FAIL)")
    delay = float(os.environ.get("KB_SIM_DELAY_S", "0") or 0)
    if delay > 0:
        time.sleep(delay)

    retrieval_query = _resolve_retrieval_query(question, conversation_history)
    target_pattern = (target_pattern or "").strip()
    # The real client blends the two in exactly this shape; `search_text` is
    # what actually gets matched, which is why the tool surfaces it when it
    # differs from `retrieval_query`.
    search_text = (
        f"{retrieval_query} | Likely matching historical pattern: "
        f"{target_pattern}" if target_pattern else retrieval_query)

    if os.environ.get("KB_SIM_EMPTY"):
        return {"answer": "", "retrieval_query": retrieval_query,
                "search_text": search_text,
                "matched_clusters": [], "relevant_bullets": []}

    query_tokens = _tokens(search_text)
    scored: list[tuple[float, dict, list[dict]]] = []
    for cluster in _CLUSTERS:
        cluster_similarity = _overlap(query_tokens, _tokens(cluster["cluster_text"]))
        bullets = []
        for bullet in cluster["bullets"]:
            similarity = _overlap(
                query_tokens, _tokens(f"{bullet['text']} {bullet['rationale']}"))
            bullets.append({**bullet, "similarity": similarity})
        bullet_cluster_score = max((b["similarity"] for b in bullets), default=0.0)
        final_score = round(0.6 * cluster_similarity + 0.4 * bullet_cluster_score, 3)
        if final_score <= 0:
            continue
        scored.append((final_score, {
            "cluster_key": cluster["cluster_key"],
            "pattern_type": cluster["pattern_type"],
            "cluster_text": cluster["cluster_text"],
            "cluster_similarity": cluster_similarity,
            "bullet_cluster_score": bullet_cluster_score,
            "final_score": final_score,
        }, bullets))

    scored.sort(key=lambda row: row[0], reverse=True)
    scored = scored[:3]

    matched_clusters = [row[1] for row in scored]
    relevant_bullets: list[dict] = []
    for _, cluster, bullets in scored:
        for bullet in sorted(bullets, key=lambda b: b["similarity"], reverse=True):
            if bullet["similarity"] <= 0:
                continue
            relevant_bullets.append({
                "cluster_key": cluster["cluster_key"],
                "pattern_type": cluster["pattern_type"],
                "cluster_text": cluster["cluster_text"],
                "case_id": bullet["case_id"],
                "text": bullet["text"],
                "rationale": bullet["rationale"],
                "similarity": bullet["similarity"],
                "raw_quote": bullet["raw_quote"],
            })

    return {
        "answer": _compose_answer(matched_clusters, relevant_bullets),
        "retrieval_query": retrieval_query,
        "search_text": search_text,
        "matched_clusters": matched_clusters,
        "relevant_bullets": relevant_bullets,
    }


def _compose_answer(clusters: list[dict], bullets: list[dict]) -> str:
    """Deterministic prose over what was retrieved — no model involved."""
    if not clusters:
        return "No prior case in the knowledge base matches this framing."
    cases = sorted({b["case_id"] for b in bullets})
    lead = clusters[0]
    kind = ("a COMMON pattern across prior cases" if lead["pattern_type"] == "common"
            else "a pattern that SINGLES OUT the cases carrying it")
    lines = [
        f"{len(cases)} prior case(s) match: {', '.join(cases)}. "
        f"The strongest match is {kind} — {lead['cluster_text'].lower()}."
    ]
    for bullet in bullets[:3]:
        lines.append(f"- {bullet['case_id']}: {bullet['text']} "
                     f"(\"{bullet['raw_quote']}\")")
    if len(clusters) > 1:
        lines.append("Also matched: "
                     + "; ".join(f"[{c['pattern_type']}] {c['cluster_text']}"
                                 for c in clusters[1:]) + ".")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover — manual check
    import json
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "any other similar cases like this one?"
    pattern = sys.argv[2] if len(sys.argv) > 2 else (
        "revolving balance near limit for 6+ months with minimum-due-only "
        "payments, ending in default")
    print(json.dumps(
        answer_question(json_path="(unused by the simulation)", question=q,
                        conversation_history=[], target_pattern=pattern),
        indent=2, ensure_ascii=False))
