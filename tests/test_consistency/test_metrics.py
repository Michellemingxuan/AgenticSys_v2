from tests.test_consistency.metrics import (
    aggregate_runs,
    extract_event_metrics,
    extract_trace_metrics,
    score_content,
)
from tests.test_consistency.score_reviews import aggregate_reviews


def test_event_metrics_extract_team_subqueries_and_provenance():
    events = [
        ("team_plan", {"tool_calls": [
            {"call_id": "c1", "tool": "modeling", "sub_question": "TSR in 2025?"},
            {"call_id": "c2", "tool": "report_agent", "sub_question": "reports?"},
        ]}),
        ("agent_completed", {
            "call_id": "c1", "tool": "modeling", "payload": {
                "scope": "model_scores_transaction: 2025-01..2025-12",
                "measured_over": [
                    "summarize_trend(model_scores_transaction.tsr, filters=2025)"
                ],
            },
        }),
        ("agent_completed", {
            "call_id": "c2", "tool": "report_agent", "payload": {"coverage": "partial"},
        }),
    ]
    out = extract_event_metrics(events)
    assert out["team_unique"] == ["modeling", "report_agent"]
    assert out["subqueries"]["modeling"] == "TSR in 2025?"
    assert out["measured_tools"] == ["summarize_trend"]
    assert out["provenance_completeness"] == 1.0


def test_trace_metrics_counts_leaf_tokens_retry_and_memory():
    rows = [
        {"id": 1, "parent_id": None, "node": "specialist.modeling",
         "depth": 0, "tags": '["kp_digest_present"]', "outcome": "ok"},
        {"id": 2, "parent_id": 1, "node": "specialist.modeling.round_1",
         "depth": 1, "prompt_tokens": 100, "completion_tokens": 10,
         "total_tokens": 110, "cached_input_tokens": 20, "outcome": "ok",
         "output_json": {"choices": [{"message": {"tool_calls": [
             {"type": "function", "id": "kb1",
              "function": {"name": "kp_lookup", "arguments": "{}"}}
         ]}}]}},
        {"id": 3, "parent_id": None, "node": "specialist.modeling.retry",
         "depth": 0, "tags": '["retry"]', "outcome": "ok"},
        {"id": 4, "parent_id": 3, "node": "specialist.modeling.retry.round_1",
         "depth": 1, "prompt_tokens": 50, "completion_tokens": 5,
         "total_tokens": 55, "outcome": "ok",
         "messages_json": [{"type": "function_call_output", "call_id": "kb1",
                            "output": "{\"topic\": \"tsr\", \"claim\": \"up\"}"}]},
        {"id": 5, "parent_id": None, "node": "cache_replay", "depth": 0,
         "tags": '["cache_hit", "exact"]', "outcome": "ok"},
    ]
    out = extract_trace_metrics(rows)
    assert out["llm_call_count"] == 2
    assert out["total_tokens"] == 165
    assert out["retry_count"] == 1
    assert out["qa_cache_hit"] is True
    assert out["kp_context_exposures"] == 1
    assert out["kp_lookup_hits"] == 1


def test_content_score_uses_scope_and_answer_contract():
    run = {
        "outcome": "ok",
        "final_answer": "There were two returned payments.",
        "team_unique": ["spend_payments", "report_agent"],
        "scopes": ["payments: 2025-01..2025-12"],
        "measured_over": ["query_table(payments.status, filters=returned)"],
        "provenance_completeness": 1.0,
    }
    cfg = {
        "expected_outcome": "ok",
        "required_specialists": ["spend_payments"],
        "allowed_specialists": ["spend_payments", "report_agent"],
        "required_scope_terms": ["payments", "2025"],
        "answer_must_include": ["returned"],
        "answer_must_not_include": ["unable"],
    }
    scored = score_content(run, cfg)
    assert scored["automated_content_score"] == 100.0


def test_content_score_is_unset_without_a_question_contract():
    run = {
        "outcome": "ok", "final_answer": "x", "team_unique": ["modeling"],
        "scopes": ["scores: all dates"], "measured_over": ["query_table(scores)"],
        "provenance_completeness": 1.0,
    }
    assert score_content(run, None)["automated_content_score"] is None


def test_aggregate_consistency_latency_retry_and_memory():
    base = {
        "mode": "cold", "name": "q", "outcome": "ok",
        "team_unique": ["modeling"], "data_tools": ["query_table"],
        "measured_tools": [], "subqueries": {"modeling": "check tsr trend"},
        "total_tokens": 100, "llm_call_count": 3, "retried": False,
        "qa_cache_hit": False, "kp_context_exposures": 0,
        "kp_lookup_calls": 0, "kp_lookup_hits": 0,
        "provenance_completeness": 1.0, "automated_content_score": 90,
    }
    runs = [
        {**base, "run_index": 1, "elapsed_seconds": 2.0},
        {**base, "run_index": 2, "elapsed_seconds": 2.2},
    ]
    out = aggregate_runs(runs)["questions"][0]
    assert out["team_exact_consistency"] == 1.0
    assert out["tool_pairwise_jaccard"] == 1.0
    assert out["subquery_pairwise_similarity"] == 1.0
    assert out["retry_rate"] == 0.0
    assert out["automated_content_score"] == 90


def test_aggregate_human_reviews():
    reviews = [{
        "review_id": "R1", "correctness_1_5": "5", "completeness_1_5": "4",
        "relevance_1_5": "5", "clarity_1_5": "4",
        "uncertainty_calibration_1_5": "3", "scope_correct_yes_no": "yes",
        "unsupported_claims": "",
    }]
    keys = [{"review_id": "R1", "mode": "cold", "name": "q"}]
    out = aggregate_reviews(reviews, keys)
    assert out["n_reviewed"] == 1
    assert out["overall"]["correctness_1_5"] == 5
    assert out["overall"]["scope_correct_rate"] == 1.0
    assert out["overall"]["unsupported_claim_rate"] == 0.0
