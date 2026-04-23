"""Tool-callable helper skills, wired for LangChain bind_tools().

Returns a list of plain Python callables that FirewalledModel.ainvoke
can bind_tools() on. Using plain callables (rather than `@tool`-decorated
LangChain Tool objects) keeps the dependency on langchain_core's tools
module optional and lets our `_tool_loop` introspect signatures via the
existing bind_tools code path.

Docstrings on each helper are pulled verbatim from the corresponding
markdown skill body at build time so the LLM sees the same guidance
whether the skill is invoked inline (prompt-injected) or as a tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from skills.loader import load_skill as _load_skill
from tools.acropedia import acropedia_lookup as _acropedia_lookup


_HELPER_DIR = Path(__file__).parent.parent / "skills" / "helper"


def _with_doc(fn: Callable, skill_body: str) -> Callable:
    """Attach the skill body as the function's docstring so bind_tools picks
    it up as the tool description. Returns a thin wrapper so we don't
    mutate the original function's __doc__ (which is shared).
    """
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = skill_body
    wrapper.__wrapped__ = fn  # for inspect / debugging
    return wrapper


def acropedia_lookup(term: str) -> dict:
    """Placeholder docstring — overwritten by the skill body in build_helper_tools()."""
    return _acropedia_lookup(term)


def web_browser(url: str) -> str:
    """Placeholder docstring — overwritten by the skill body in build_helper_tools()."""
    return (
        "web browser not yet available — the helper is a placeholder while "
        "the fetch infrastructure is wired in. URL was: " + (url or "")
    )


def build_helper_tools() -> list[Callable]:
    """Return the list of tool-callable helper skills for bind_tools().

    Each returned callable carries the corresponding markdown skill body as
    its docstring so the LLM reads the same instructions whether the skill
    is loaded inline or exposed via tool-calling.
    """
    acropedia_body = _load_skill(_HELPER_DIR / "acropedia.md").body
    web_body = _load_skill(_HELPER_DIR / "web_browser.md").body
    return [
        _with_doc(acropedia_lookup, acropedia_body),
        _with_doc(web_browser, web_body),
    ]
