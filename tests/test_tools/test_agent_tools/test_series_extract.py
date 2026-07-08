"""Tests for tools/series_extract.py.

Extracted from tests/test_agent_factories/test_redacting_tool.py per the
redacting_tool decomposition plan (task 1). No tests were found in
test_redacting_tool.py that directly exercised _parse_series_from_tool_outputs,
_fill_kp_numbers, _values_match, _ParsedSeries, or _extract_data_tool_outputs
by name at the time of extraction — those functions were tested indirectly
through the higher-level redacting_tool integration tests.
"""
from tools.series_extract import (  # noqa: F401
    _ParsedSeries,
    _extract_data_tool_outputs,
    _fill_kp_numbers,
    _parse_series_from_tool_outputs,
    _values_match,
)
