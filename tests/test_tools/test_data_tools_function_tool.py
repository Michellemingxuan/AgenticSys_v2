"""Verify that data_tools functions are exposed as Agents SDK function_tools."""
from tools.data_tools import list_available_tables, get_table_schema, query_table


def test_list_available_tables_is_function_tool():
    # The @function_tool decorator wraps the callable; the result is a
    # FunctionTool dataclass (not directly callable) but exposes SDK-recognized
    # metadata including .name.
    assert hasattr(list_available_tables, "name")
    assert list_available_tables.name == "list_available_tables"


def test_get_table_schema_is_function_tool():
    assert hasattr(get_table_schema, "name")
    assert get_table_schema.name == "get_table_schema"


def test_query_table_is_function_tool():
    assert hasattr(query_table, "name")
    assert query_table.name == "query_table"
