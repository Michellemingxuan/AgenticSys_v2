import json
import os
import pytest
from logger.event_logger import EventLogger


@pytest.fixture
def logger(tmp_path):
    log = EventLogger(session_id="test-session-001", log_dir=str(tmp_path))
    return log


def test_log_creates_file(logger, tmp_path):
    logger.log("session_start", {"pillar": "credit_risk"})
    log_file = tmp_path / "test-session-001.jsonl"
    assert log_file.exists()


def test_log_writes_valid_jsonl(logger, tmp_path):
    logger.log("session_start", {"pillar": "credit_risk"})
    logger.log("orchestrator_dispatch", {"question": "test?", "specialists": ["bureau"]})
    log_file = tmp_path / "test-session-001.jsonl"
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        event = json.loads(line)
        assert "timestamp" in event
        assert "session_id" in event
        assert "event" in event


def test_log_includes_trace_id(logger, tmp_path):
    logger.set_trace("q-001")
    logger.log("data_request", {"domain": "bureau", "intent": "delinquency count"})
    log_file = tmp_path / "test-session-001.jsonl"
    event = json.loads(log_file.read_text().strip())
    assert event["trace_id"] == "q-001"


def test_log_without_trace_id(logger, tmp_path):
    logger.log("session_start", {"pillar": "credit_risk"})
    log_file = tmp_path / "test-session-001.jsonl"
    event = json.loads(log_file.read_text().strip())
    assert event["trace_id"] is None


def test_multiple_traces(logger, tmp_path):
    logger.set_trace("q-001")
    logger.log("data_request", {"domain": "bureau"})
    logger.set_trace("q-002")
    logger.log("data_request", {"domain": "modeling"})
    log_file = tmp_path / "test-session-001.jsonl"
    lines = log_file.read_text().strip().split("\n")
    assert json.loads(lines[0])["trace_id"] == "q-001"
    assert json.loads(lines[1])["trace_id"] == "q-002"
