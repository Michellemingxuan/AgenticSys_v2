import json
import pytest
from agent_factories.general_specialist import build_general_specialist
from models.types import ReviewReport, ReviewDirective


def test_review_report_parses_directive_from_model_json():
    # The reviewer's output_type is ReviewReport; assert a directive-bearing
    # ReviewReport round-trips through model_validate (what the SDK does).
    payload = {
        "resolved": [], "open_conflicts": [],
        "directive": {"kind": "needs_redispatch", "specialist": "modeling",
                      "anchor": "2025-05", "why": "drivers not anchored to spike"},
    }
    report = ReviewReport.model_validate(payload)
    assert report.directive.kind == "needs_redispatch"
    assert report.directive.specialist == "modeling"


def test_build_general_specialist_output_type_is_review_report():
    # model=None: agent construction must not call the model during wiring.
    # The Agent SDK accepts None; _M() was rejected (not a Model subclass).
    agent = build_general_specialist(model=None)
    # AgentOutputSchema wraps ReviewReport; confirmed accessor via
    # dir(agent.output_type): the real attribute is `.output_type`.
    assert agent.output_type.output_type is ReviewReport
