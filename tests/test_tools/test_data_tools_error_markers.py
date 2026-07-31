"""Drift guard: every error string data_tools.py actually emits must still be
classified by grounding.classify_tool_output.

This parses the literals out of the SOURCE rather than hard-coding copies — a
hard-coded copy would keep passing after someone reworded the real message,
which is precisely the drift this guards against.
"""
import ast
import pathlib

import pytest

from agent_factories.agent_tools.grounding import classify_tool_output

_SRC = pathlib.Path(__file__).resolve().parents[2] / "tools" / "data_tools.py"

# Marker substrings that identify a string literal as an error message.
_ERROR_MARKERS = ("Data unavailable", "no parseable", "did NOT run")

# Map the private impl function that owns a literal to the public tool name the
# specialist sees. classify_tool_output keys the get_table_schema carve-out on
# the tool name, so this mapping is load-bearing.
_IMPL_TO_TOOL = {
    "_list_available_tables_impl": "list_available_tables",
    "_get_table_schema_impl": "get_table_schema",
    "_query_table_impl": "query_table",
    "_batch_query_table_impl": "batch_query_table",
    "_join_table_impl": "join_table",
    "_transaction_detail_impl": "transaction_detail",
    "_aggregate_column_impl": "aggregate_column",
    "_batch_aggregate_impl": "batch_aggregate",
    "_summarize_trend_impl": "summarize_trend",
    "_batch_summarize_trend_impl": "batch_summarize_trend",
    "_summarize_by_group_impl": "summarize_by_group",
    "_search_columns_impl": "search_columns",
}

# Shared error helpers that are NOT `_*_impl` functions but DO own marker
# literals. `_unparseable_specs_directive` (data_tools.py:2820) is the ONLY
# home of the "did NOT run" literal, and it is called by the three batch impls
# rather than inlined into them — so a walk that only descends into `_*_impl`
# functions misses `specs_unparseable` entirely. That is the marker for the
# truncated-specs_json → fabricated-peaks bug this whole feature exists to
# catch, so it must be covered. It takes `tool` as a parameter, so its literals
# are checked against every tool it serves.
_SHARED_HELPERS = {
    "_unparseable_specs_directive": [
        "batch_query_table", "batch_aggregate", "batch_summarize_trend",
    ],
}


def _render(node) -> str | None:
    """Reconstruct a str/f-string literal, substituting 'X' for interpolations.

    f"table '{name}' not found" -> "table 'X' not found", which is exactly the
    shape classify_tool_output must match.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("X")
        return "".join(parts)
    return None


def _iter_nodes_no_joinedstr_descent(node):
    """Like ast.walk, but does not descend into a JoinedStr's own children.

    `ast.walk` visits a JoinedStr node AND, separately, each Constant piece
    inside it (the text either side of an f-string interpolation). _render
    already reconstructs the JoinedStr's full text in one pass, so descending
    further would also yield those Constant pieces on their own — fragments
    like "Data unavailable: table '" with no closing quote or suffix, which
    data_tools.py never emits as a standalone string. Treating JoinedStr as a
    leaf for this walk avoids collecting those fragments as fake literals.
    """
    yield node
    if isinstance(node, ast.JoinedStr):
        return
    for child in ast.iter_child_nodes(node):
        yield from _iter_nodes_no_joinedstr_descent(child)


def _collect():
    """[(tool_name, literal), ...] for every error literal in data_tools.py."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        tools = _SHARED_HELPERS.get(fn.name)
        if tools is None:
            tool = _IMPL_TO_TOOL.get(fn.name)
            if tool is None:
                continue
            tools = [tool]
        for node in _iter_nodes_no_joinedstr_descent(fn):
            text = _render(node)
            if text and any(m in text for m in _ERROR_MARKERS):
                found.extend((t, text) for t in tools)
    return found


_CASES = _collect()


def test_collection_found_the_expected_literals():
    """Guards the guard: if the AST walk silently stops finding literals (file
    moved, functions renamed), every parametrized case below would vacuously
    pass. data_tools.py had 16 'Data unavailable' sites plus 3 others when this
    was written; require a healthy floor."""
    assert len(_CASES) >= 15, (
        f"only found {len(_CASES)} error literals in {_SRC} — the AST walk or "
        f"_IMPL_TO_TOOL is probably stale"
    )


@pytest.mark.parametrize("tool,literal", _CASES,
                         ids=[f"{t}:{lit[:40]}" for t, lit in _CASES])
def test_every_error_literal_is_classified(tool, literal):
    """Except the deliberate carve-out: a DISCOVERY tool reporting a table
    absent from this case is benign exploration, not a failure.

    Read from grounding's own set rather than naming the tools here — this file
    exists because hard-coded copies keep passing after the real thing changes.
    """
    from agent_factories.agent_tools.grounding import _SCHEMA_PROBE_TOOLS

    reason = classify_tool_output(tool, literal)
    is_carve_out = (
        tool in _SCHEMA_PROBE_TOOLS
        and "not found for current case" in literal
    )
    if is_carve_out:
        assert reason is None, (
            f"the {tool} discovery carve-out should suppress {literal!r}"
        )
    else:
        assert reason is not None, (
            f"data_tools.py emits {literal!r} from {tool}, but "
            f"classify_tool_output does not recognize it — a marker in "
            f"grounding.py has drifted from the source string"
        )


def test_specs_unparseable_marker_is_covered():
    """The `did NOT run` literal lives ONLY in the shared
    `_unparseable_specs_directive` helper, not inline in any `_*_impl`. It is
    the marker for the truncated-specs_json → fabricated-peaks failure that
    motivated this feature, so a walk that silently stops covering it would
    leave the most important marker unguarded while still reporting green."""
    covered = {tool for tool, lit in _CASES if "did NOT run" in lit}
    assert covered == {
        "batch_query_table", "batch_aggregate", "batch_summarize_trend",
    }, f"specs_unparseable marker not collected for the batch tools: {covered}"


# ── DATA GAP is a benign negative, not an error ─────────────────────────────
#
# "the column is empty for THIS case" is the tool WORKING and reporting an
# absence — same category as get_table_schema saying a table isn't present.
# Flagging it made a specialist that honestly reported the gap indistinguishable
# from one that fabricated numbers, and quarantined the honest one (observed on
# case 366132845011: bureau_data.'SBFE Score' is blank in all 26 rows).

def test_data_gap_marker_is_still_emitted_by_data_tools():
    """Binds READER to WRITER. If the message is reworded without updating
    grounding, the gap silently becomes a 'failed tool call' again."""
    from agent_factories.agent_tools.grounding import _DATA_GAP_MARKER

    src = _SRC.read_text()
    assert _DATA_GAP_MARKER in src, (
        f"grounding treats {_DATA_GAP_MARKER!r} as benign, but data_tools no "
        f"longer emits it — one side was reworded without the other")


def test_data_gap_output_is_not_classified_as_a_failure():
    out = ("trend(max(SBFE Score) by month on month) = (DATA GAP: column "
           "'SBFE Score' is EMPTY for this case — 26 row(s) in range, every one "
           "blank or non-numeric; the month values parsed fine. The tool "
           "worked; this case has no SBFE Score data. Record it in `data_gaps` "
           "and do NOT retry this column.)")
    assert classify_tool_output("summarize_trend", out) is None


def test_an_unparseable_DATE_column_is_still_a_failure():
    """The carve-out must not swallow the real thing it was carved out of."""
    out = ("trend(max(FICO Score) by month on FICO Score) = (no parseable "
           "FICO Score values; 26 total in bureau_data)")
    assert classify_tool_output("summarize_trend", out) == "no_buckets"
