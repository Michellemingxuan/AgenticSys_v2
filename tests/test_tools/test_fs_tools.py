"""Tests for fs_tools.

The @function_tool decorator wraps the underlying coroutines in a FunctionTool
dataclass that is not directly callable. The SDK invokes the tool at runtime
via `FunctionTool.on_invoke_tool(ctx, json_str)` where ctx is a
RunContextWrapper. We replicate that calling convention here.
"""
import json
import pytest
from pathlib import Path
from agents import RunContextWrapper
from tools.fs_tools import fs_grep, fs_list_files, fs_read_file
from models.app_context import AppContext


@pytest.mark.asyncio
async def test_fs_list_files_returns_files_in_case_folder(tmp_path):
    (tmp_path / "credit_review.md").write_text("content")
    (tmp_path / "summary.txt").write_text("more")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_list_files.on_invoke_tool(ctx, "{}")
    assert "credit_review.md" in out
    assert "summary.txt" in out


@pytest.mark.asyncio
async def test_fs_read_file_reads_named_file(tmp_path):
    (tmp_path / "report.md").write_text("Top finding: X.")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(ctx, json.dumps({"filename": "report.md"}))
    assert "Top finding: X." in out


@pytest.mark.asyncio
async def test_fs_read_file_rejects_path_traversal(tmp_path):
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(ctx, json.dumps({"filename": "../etc/passwd"}))
    assert "denied" in out.lower() or "invalid" in out.lower()


@pytest.mark.asyncio
async def test_fs_grep_or_matching_and_ranking(tmp_path):
    # file_hi matches 3 distinct terms across 2 lines; file_lo matches 1.
    (tmp_path / "file_hi.md").write_text(
        "Recurring spend pattern observed.\n"
        "A high-value transaction of 174897 posted.\n"
    )
    (tmp_path / "file_lo.md").write_text("Only one recurring note here.\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(
        ctx, json.dumps({"terms": ["spend", "transaction", "recurring"]})
    )
    # both files surface
    assert "file_hi.md" in out and "file_lo.md" in out
    # file_hi (3 distinct terms) ranks above file_lo (1 term)
    assert out.index("file_hi.md") < out.index("file_lo.md")
    # line-numbered snippets present
    assert "L1:" in out and "L2:" in out
    # numeric parity with fs_read_file's comma formatting
    assert "174,897" in out


@pytest.mark.asyncio
async def test_fs_grep_is_case_insensitive(tmp_path):
    (tmp_path / "r.md").write_text("Total SPEND was high.\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(ctx, json.dumps({"terms": ["spend"]}))
    assert "r.md" in out


@pytest.mark.asyncio
async def test_fs_grep_no_matches_signal(tmp_path):
    (tmp_path / "r.md").write_text("nothing relevant here\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(ctx, json.dumps({"terms": ["bureau", "fico"]}))
    assert out.startswith("No matches for:")
    assert "bureau" in out and "fico" in out


@pytest.mark.asyncio
async def test_fs_grep_empty_terms(tmp_path):
    (tmp_path / "r.md").write_text("anything\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_grep.on_invoke_tool(ctx, json.dumps({"terms": []}))
    assert out == "No terms provided."


_FIVE_LINES = (
    "line one alpha\n"
    "line two beta\n"
    "line three gamma\n"
    "line four delta\n"
    "line five epsilon\n"
)


@pytest.mark.asyncio
async def test_fs_read_file_whole_file_default_unchanged(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(ctx, json.dumps({"filename": "r.md"}))
    assert "alpha" in out and "epsilon" in out  # all lines present


@pytest.mark.asyncio
async def test_fs_read_file_range_returns_window(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "r.md", "start_line": 2, "end_line": 4})
    )
    assert "beta" in out and "gamma" in out and "delta" in out
    assert "alpha" not in out and "epsilon" not in out


@pytest.mark.asyncio
async def test_fs_read_file_range_clamps_out_of_bounds(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "r.md", "start_line": 4, "end_line": 999})
    )
    assert "delta" in out and "epsilon" in out
    assert "gamma" not in out


@pytest.mark.asyncio
async def test_fs_read_file_range_inverted_bounds_swap(tmp_path):
    (tmp_path / "r.md").write_text(_FIVE_LINES)
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "r.md", "start_line": 4, "end_line": 2})
    )
    assert "beta" in out and "gamma" in out and "delta" in out
    assert "alpha" not in out and "epsilon" not in out
