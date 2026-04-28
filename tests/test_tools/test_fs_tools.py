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
from tools.fs_tools import fs_list_files, fs_read_file
from case_agents.app_context import AppContext


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
