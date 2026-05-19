import json

from tools.timing_report import summarize_timing


def test_summarize_timing_groups_phase_totals(tmp_path):
    log_path = tmp_path / "case.jsonl"
    events = [
        {"event": "process_phase_timing", "process": "turn",
         "phase": "screen", "duration_ms": 10},
        {"event": "process_phase_timing", "process": "turn",
         "phase": "screen", "duration_ms": 15},
        {"event": "process_phase_timing", "process": "specialist_call",
         "phase": "specialist_runner", "duration_ms": 50},
        {"event": "process_timing_summary", "process": "turn",
         "total_ms": 100, "phase_totals": {"screen": 25}},
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in events))

    summary = summarize_timing(log_path)

    assert summary["n_phase_events"] == 3
    assert summary["n_summary_events"] == 1
    assert summary["by_process"]["turn"]["phase_totals"]["screen"] == 25
    assert summary["by_process"]["turn"]["phase_counts"]["screen"] == 2
    assert summary["by_process"]["specialist_call"]["phase_totals"][
        "specialist_runner"
    ] == 50
