from logger.process_timer import ProcessTimer


class _Logger:
    def __init__(self):
        self.events = []

    def log(self, event_type, payload):
        self.events.append((event_type, payload))


def test_process_timer_logs_phase_and_summary():
    logger = _Logger()
    timer = ProcessTimer(logger, "turn", turn_id="t1")

    timer.record("screen", 12, passed=True)
    summary = timer.summary(outcome="ok")

    assert logger.events[0][0] == "process_phase_timing"
    assert logger.events[0][1]["process"] == "turn"
    assert logger.events[0][1]["turn_id"] == "t1"
    assert logger.events[0][1]["phase"] == "screen"
    assert logger.events[0][1]["duration_ms"] == 12

    assert logger.events[1][0] == "process_timing_summary"
    assert logger.events[1][1]["phase_totals"] == {"screen": 12}
    assert logger.events[1][1]["outcome"] == "ok"
    assert summary["n_phases"] == 1
