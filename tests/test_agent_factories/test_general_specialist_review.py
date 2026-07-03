"""Tests for general_specialist review-directive wiring.

matplotlib is not installed in the dev environment but is imported at module
level by tools/viz_renderer.py (→ tools/data_viz_tools.py → general_specialist).
Stub it here, before the factory import, so this module can be collected without
the library installed. The guard prevents shadowing a real install.
"""
from __future__ import annotations
import sys
import types


def _make_matplotlib_stub() -> types.ModuleType:
    mpl = types.ModuleType("matplotlib")
    mpl.use = lambda *a, **kw: None  # matplotlib.use("Agg")

    plt = types.ModuleType("matplotlib.pyplot")

    # Minimal stubs for anything called at *import* time in viz_renderer.
    # Actual render calls happen only inside render_chart(); none of the
    # factory-wiring tests invoke it.
    class _FuncFormatter:
        def __init__(self, fn):
            self._fn = fn

    plt.FuncFormatter = _FuncFormatter
    plt.subplots = lambda *a, **kw: (None, None)
    plt.close = lambda *a, **kw: None

    mpl.pyplot = plt
    sys.modules["matplotlib"] = mpl
    sys.modules["matplotlib.pyplot"] = plt
    return mpl


if "matplotlib" not in sys.modules:
    _make_matplotlib_stub()

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
