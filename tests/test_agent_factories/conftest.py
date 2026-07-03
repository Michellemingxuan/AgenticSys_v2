"""Conftest for test_agent_factories — stubs out optional heavy dependencies.

matplotlib is not installed in the dev environment but is imported at module
level by tools/viz_renderer.py (→ tools/data_viz_tools.py → agent_factories
that include chart tools). Stub it here before test modules are imported so
factory-wiring tests can run without the library installed.
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
