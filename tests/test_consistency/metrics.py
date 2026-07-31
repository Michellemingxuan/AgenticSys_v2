"""Pure metric extraction and aggregation for the agentic Q&A benchmark.

The live runner lives in :mod:`tests.test_consistency.evaluate`.  This module
has no LLM or application imports, which keeps the scoring rules cheap to unit
test and makes old JSONL result files re-analyzable after the benchmark.
"""
from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from itertools import combinations
from typing import Any, Iterable


AUXILIARY_AGENTS = frozenset({"report_agent", "general_specialist"})
DATA_TOOLS = frozenset({
    "query_table", "batch_query_table", "transaction_detail", "join_table",
    "aggregate_column", "batch_aggregate", "summarize_trend",
    "batch_summarize_trend", "summarize_by_group", "get_table_schema",
    "list_available_tables", "search_columns", "score_driver_values",
})

_WORD = re.compile(r"[a-z0-9_]+")
_MEASURED_TOOL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _walk(value: Any) -> Iterable[dict]:
    """Yield every dict nested in a JSON-compatible object."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _tags(row: dict) -> set[str]:
    raw = _json(row.get("tags"), [])
    return {str(v) for v in raw} if isinstance(raw, list) else set()


def _tool_transcript(trace_rows: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(call_id -> tool name, call_id -> output text)``.

    Function calls are read from ``output_json`` (where each call is emitted
    once).  Results are read from later-round ``messages_json`` and deduplicated
    by call id, avoiding the repeated-history overcount that a regex scan would
    introduce.
    """
    calls: dict[str, str] = {}
    outputs: dict[str, str] = {}
    for row in trace_rows:
        for item in _walk(_json(row.get("output_json"), {})):
            kind = item.get("type")
            function = item.get("function")
            if kind in {"function_call", "tool_call"}:
                name = item.get("name")
            elif kind == "function" and isinstance(function, dict):
                # Synthetic ChatCompletion shape produced by
                # NodeTraceRunHooks._items_to_chatcompletion_shape.
                name = function.get("name")
            else:
                continue
            call_id = item.get("call_id") or item.get("id")
            if name and call_id:
                calls[str(call_id)] = str(name)
        for item in _walk(_json(row.get("messages_json"), [])):
            if item.get("type") not in {"function_call_output", "tool_result"}:
                continue
            call_id = item.get("call_id") or item.get("tool_call_id")
            if call_id:
                outputs[str(call_id)] = str(
                    item.get("output") if "output" in item else item.get("content", "")
                )
    return calls, outputs


def extract_trace_metrics(trace_rows: list[dict]) -> dict:
    """Extract latency/cost/retry/memory telemetry from one turn's trace rows."""
    child_ids = {
        int(row["parent_id"])
        for row in trace_rows
        if row.get("parent_id") is not None
    }
    leaves = [row for row in trace_rows if int(row.get("id") or -1) not in child_ids]
    llm_rows = [
        row for row in leaves
        if any(row.get(k) is not None
               for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
    ]

    def _sum(field: str, rows: list[dict] = llm_rows) -> int:
        return sum(int(row.get(field) or 0) for row in rows)

    retry_nodes = []
    for row in trace_rows:
        name = str(row.get("node") or "")
        tags = _tags(row)
        if name.endswith(".retry") or tags.intersection(
            {"retry", "ungrounded_retry", "planning_timeout"}
        ):
            retry_nodes.append(int(row.get("id") or -1))
    # Orchestrator attempts use the same wrapper name; attempts after the first
    # are retries even when an exception prevented a retry tag from landing.
    orch_attempts = sum(
        1 for row in trace_rows
        if row.get("node") == "orchestrator" and int(row.get("depth") or 0) == 0
    )
    retry_count = len(set(retry_nodes)) + max(0, orch_attempts - 1)

    calls, outputs = _tool_transcript(trace_rows)
    data_tools = sorted({name for name in calls.values() if name in DATA_TOOLS})
    kb_ids = [cid for cid, name in calls.items() if name == "kb_lookup"]
    miss_markers = ("not found", "not in the cached working set", "kb is empty")
    kb_hits = kb_misses = kb_unknown = 0
    for cid in kb_ids:
        if cid not in outputs:
            kb_unknown += 1
        elif any(marker in outputs[cid].lower() for marker in miss_markers):
            kb_misses += 1
        else:
            kb_hits += 1

    cached_input = _sum("cached_input_tokens")
    prompt_tokens = _sum("prompt_tokens")
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": _sum("completion_tokens"),
        "total_tokens": sum(
            int(row.get("total_tokens") or (
                int(row.get("prompt_tokens") or 0)
                + int(row.get("completion_tokens") or 0)
            ))
            for row in llm_rows
        ),
        "cached_input_tokens": cached_input,
        "cached_input_token_rate": (
            cached_input / prompt_tokens if prompt_tokens else None
        ),
        "llm_call_count": len(llm_rows),
        "retry_count": retry_count,
        "retried": retry_count > 0,
        "failed_node_count": sum(
            1 for row in trace_rows if row.get("outcome") in {"failed", "timeout"}
        ),
        "qa_cache_hit": any(
            row.get("node") == "cache_replay" and "cache_hit" in _tags(row)
            for row in trace_rows
        ),
        "kb_context_exposures": sum(
            1 for row in trace_rows if "kb_digest_present" in _tags(row)
        ),
        "kb_lookup_calls": len(kb_ids),
        "kb_lookup_hits": kb_hits,
        "kb_lookup_misses": kb_misses,
        "kb_lookup_unknown": kb_unknown,
        "data_tools": data_tools,
    }


def extract_event_metrics(events: list[tuple[str, dict]]) -> dict:
    """Extract final team, sub-questions and provenance from captured SSE."""
    plans = [payload for event, payload in events if event == "team_plan"]
    final_plan = (plans[-1].get("tool_calls") or []) if plans else []
    team = [str(call.get("tool") or "?") for call in final_plan]
    subqueries = {
        str(call.get("tool") or "?"): str(call.get("sub_question") or "")
        for call in final_plan
    }

    completed: dict[str, tuple[str, dict]] = {}
    for event, payload in events:
        if event != "agent_completed":
            continue
        call_id = str(payload.get("call_id") or len(completed))
        body = payload.get("payload")
        body = body if isinstance(body, dict) else _json(body, {})
        completed[call_id] = (str(payload.get("tool") or "?"), body)

    scopes: list[str] = []
    measured: list[str] = []
    provenance_eligible = provenance_complete = 0
    measured_tools: set[str] = set()
    for tool, body in completed.values():
        if tool in AUXILIARY_AGENTS or tool == "?":
            continue
        provenance_eligible += 1
        scope = str(body.get("scope") or "").strip()
        lines = body.get("measured_over") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(line).strip() for line in lines if str(line).strip()]
        if scope:
            scopes.append(scope)
        measured.extend(lines)
        if scope and lines:
            provenance_complete += 1
        for line in lines:
            match = _MEASURED_TOOL.match(line)
            if match:
                measured_tools.add(match.group(1))

    return {
        "team": team,
        "team_unique": sorted(set(team)),
        "subqueries": subqueries,
        "scopes": scopes,
        "measured_over": measured,
        "measured_tools": sorted(measured_tools),
        "provenance_eligible": provenance_eligible,
        "provenance_complete": provenance_complete,
        "provenance_completeness": (
            provenance_complete / provenance_eligible
            if provenance_eligible else None
        ),
    }


def score_content(run: dict, evaluation: dict | None) -> dict:
    """Score deterministic content checks configured for a question.

    This intentionally does *not* pretend that scope telemetry proves factual
    correctness.  It scores only configured, auditable checks; correctness and
    completeness still belong in the blinded review sheet.
    """
    cfg = evaluation or {}
    answer = str(run.get("final_answer") or "")
    answer_l = answer.lower()
    team = set(run.get("team_unique") or run.get("team") or [])
    provenance = run.get("provenance_completeness")
    scope_blob = " ".join(
        [*(run.get("scopes") or []), *(run.get("measured_over") or [])]
    ).lower()

    components: dict[str, dict[str, Any]] = {}

    if cfg.get("expected_outcome"):
        passed = run.get("outcome") == cfg["expected_outcome"]
        components["outcome"] = {"score": float(passed), "weight": 10}

    required = set(cfg.get("required_specialists") or [])
    allowed = set(cfg.get("allowed_specialists") or [])
    if required or allowed:
        required_recall = len(team & required) / len(required) if required else 1.0
        precision = (
            len(team & allowed) / len(team)
            if allowed and team
            else (1.0 if not allowed or not required else 0.0)
        )
        team_score = (
            2 * required_recall * precision / (required_recall + precision)
            if allowed and required_recall + precision else required_recall
        )
        components["team"] = {
            "score": team_score,
            "weight": 15,
            "missing": sorted(required - team),
            "unexpected": sorted(team - allowed) if allowed else [],
        }

    scope_terms = [str(v).lower() for v in cfg.get("required_scope_terms") or []]
    if scope_terms:
        missing = [term for term in scope_terms if term not in scope_blob]
        components["scope_alignment"] = {
            "score": (len(scope_terms) - len(missing)) / len(scope_terms),
            "weight": 25,
            "missing": missing,
        }

    must = [str(v).lower() for v in cfg.get("answer_must_include") or []]
    any_groups = cfg.get("answer_must_include_any") or []
    forbidden = [str(v).lower() for v in cfg.get("answer_must_not_include") or []]
    checks: list[bool] = [term in answer_l for term in must]
    checks.extend(
        any(str(option).lower() in answer_l for option in group)
        for group in any_groups
    )
    checks.extend(term not in answer_l for term in forbidden)
    if checks:
        components["answer_requirements"] = {
            "score": sum(checks) / len(checks),
            "weight": 30,
            "n_checks": len(checks),
            "n_passed": sum(checks),
        }

    if provenance is not None:
        components["provenance"] = {
            "score": float(provenance),
            "weight": 20,
        }

    denom = sum(v["weight"] for v in components.values())
    score = (
        100 * sum(v["score"] * v["weight"] for v in components.values()) / denom
        if denom and cfg else None
    )
    return {"automated_content_score": score, "content_components": components}


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    aa, bb = set(a), set(b)
    if not aa and not bb:
        return 1.0
    return len(aa & bb) / len(aa | bb)


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _pairwise_subquery_similarity(a: dict[str, str], b: dict[str, str]) -> float:
    tools = set(a) | set(b)
    if not tools:
        return 1.0
    return statistics.mean(
        _jaccard(_tokens(a.get(tool, "")), _tokens(b.get(tool, "")))
        if tool in a and tool in b else 0.0
        for tool in tools
    )


def _mean_pairwise(values: list[Any], similarity) -> float | None:
    if len(values) < 2:
        return None
    return statistics.mean(similarity(a, b) for a, b in combinations(values, 2))


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _outlier_indexes(values: list[float]) -> list[int]:
    """Tukey 1.5×IQR outliers; robust and interpretable for 10 repeats."""
    if len(values) < 4:
        return []
    q1, q3 = _percentile(values, 0.25), _percentile(values, 0.75)
    assert q1 is not None and q3 is not None
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [idx for idx, value in enumerate(values) if value < lo or value > hi]


def aggregate_runs(runs: list[dict]) -> dict:
    """Aggregate raw run records by experiment mode and question name."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run in runs:
        grouped[(str(run.get("mode") or "cold"), str(run["name"]))].append(run)

    questions: list[dict] = []
    for (mode, name), rows in sorted(grouped.items()):
        teams = [tuple(row.get("team_unique") or []) for row in rows]
        tools = [
            tuple(sorted(set((row.get("data_tools") or [])
                             + (row.get("measured_tools") or []))))
            for row in rows
        ]
        subqueries = [row.get("subqueries") or {} for row in rows]
        latencies = [float(row["elapsed_seconds"]) for row in rows]
        tokens = [int(row.get("total_tokens") or 0) for row in rows]
        llm_calls = [int(row.get("llm_call_count") or 0) for row in rows]
        content = [
            float(row["automated_content_score"])
            for row in rows if row.get("automated_content_score") is not None
        ]
        modal_team_n = Counter(teams).most_common(1)[0][1] if teams else 0
        any_team = any(teams)
        any_tools = any(tools)
        any_subqueries = any(subqueries)
        kb_calls = sum(int(row.get("kb_lookup_calls") or 0) for row in rows)
        kb_hits = sum(int(row.get("kb_lookup_hits") or 0) for row in rows)
        outliers = _outlier_indexes(latencies)
        questions.append({
            "mode": mode,
            "name": name,
            "n_runs": len(rows),
            "completion_rate": sum(row.get("outcome") in {"ok", "out_of_scope"}
                                   for row in rows) / len(rows),
            "team_exact_consistency": modal_team_n / len(rows) if any_team else None,
            "team_pairwise_jaccard": (
                _mean_pairwise(teams, lambda a, b: _jaccard(a, b))
                if any_team else None
            ),
            "tool_pairwise_jaccard": (
                _mean_pairwise(tools, lambda a, b: _jaccard(a, b))
                if any_tools else None
            ),
            "subquery_pairwise_similarity": (
                _mean_pairwise(subqueries, _pairwise_subquery_similarity)
                if any_subqueries else None
            ),
            "latency_seconds": {
                "mean": statistics.mean(latencies),
                "median": statistics.median(latencies),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies),
                "outlier_count": len(outliers),
                "outlier_run_indexes": [rows[i].get("run_index") for i in outliers],
            },
            "tokens": {
                "mean": statistics.mean(tokens),
                "median": statistics.median(tokens),
                "p95": _percentile([float(v) for v in tokens], 0.95),
            },
            "llm_calls": {
                "mean": statistics.mean(llm_calls),
                "max": max(llm_calls),
            },
            "retry_rate": sum(bool(row.get("retried")) for row in rows) / len(rows),
            "qa_cache_hit_rate": (
                sum(bool(row.get("qa_cache_hit")) for row in rows) / len(rows)
            ),
            "kb_context_exposure_rate": (
                sum(bool(row.get("kb_context_exposures")) for row in rows)
                / len(rows)
            ),
            "kb_lookup_hit_rate": kb_hits / kb_calls if kb_calls else None,
            "provenance_completeness": statistics.mean(
                float(row["provenance_completeness"])
                for row in rows if row.get("provenance_completeness") is not None
            ) if any(row.get("provenance_completeness") is not None for row in rows)
            else None,
            "automated_content_score": statistics.mean(content) if content else None,
        })

    return {
        "n_runs": len(runs),
        "n_questions": len({row["name"] for row in runs}),
        "questions": questions,
    }
