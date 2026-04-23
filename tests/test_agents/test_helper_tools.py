"""Tests for agents.helper_tools."""

from __future__ import annotations

from agents.helper_tools import build_helper_tools


def test_build_helper_tools_returns_callables():
    tools = build_helper_tools()
    names = [t.__name__ for t in tools]

    assert "acropedia_lookup" in names
    assert "web_browser" in names


def test_helper_tool_doc_is_skill_body():
    """Each helper's __doc__ is sourced from the corresponding .md skill body
    so the LLM sees the same guidance inline or tool-bound."""
    tools = build_helper_tools()
    by_name = {t.__name__: t for t in tools}

    acro_doc = by_name["acropedia_lookup"].__doc__
    assert acro_doc is not None
    assert "Acropedia" in acro_doc or "lookup" in acro_doc.lower()

    web_doc = by_name["web_browser"].__doc__
    assert web_doc is not None
    assert "Web" in web_doc or "url" in web_doc.lower()


def test_acropedia_tool_delegates_to_underlying_lookup():
    tools = build_helper_tools()
    acro = next(t for t in tools if t.__name__ == "acropedia_lookup")

    out = acro("DTI")
    assert out["full_name"] == "Debt-To-Income Ratio"


def test_web_browser_stub_returns_not_available_message():
    tools = build_helper_tools()
    web = next(t for t in tools if t.__name__ == "web_browser")

    out = web("https://example.com/something")
    assert "not yet available" in out.lower()
    assert "https://example.com/something" in out
