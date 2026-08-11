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


@pytest.mark.asyncio
async def test_fs_read_file_reports_binary_instead_of_raising(tmp_path):
    """A real case folder can hold a `.docx` export (case 11854808010 does).
    `read_text()` on a zip container raises UnicodeDecodeError — uncaught,
    that propagates out of the tool and kills the whole specialist turn over
    one bad filename. Degrade to a message the model can act on."""
    # Minimal zip/docx signature — enough to fail utf-8 decoding.
    (tmp_path / "report.docx").write_bytes(b"PK\x03\x04\x14\x00\x00\x00\x08\x00\xff\xfe")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "report.docx"}))
    assert "report.docx" in out
    assert "not a text file" in out.lower()
    assert "`.md`" in out
    # It must READ as a dead end, not as an empty report.
    assert out.strip() != ""


@pytest.mark.asyncio
async def test_fs_read_file_binary_guard_does_not_affect_text_files(tmp_path):
    """The guard is a fallback, not a filter — normal reads are unchanged,
    including the ranged form."""
    (tmp_path / "r.md").write_text("l1\nl2\nl3\n")
    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    whole = await fs_read_file.on_invoke_tool(ctx, json.dumps({"filename": "r.md"}))
    assert "l1" in whole and "l3" in whole
    ranged = await fs_read_file.on_invoke_tool(
        ctx, json.dumps({"filename": "r.md", "start_line": 2, "end_line": 2}))
    assert ranged.strip() == "l2"


# ── One definition of "report file", shared by all three surfaces ───────────


@pytest.mark.asyncio
async def test_fs_list_files_shows_only_readable_report_files(tmp_path):
    """The case folder accumulates more than reports: a `charts/` subdir and
    binary exports the reader cannot open. Listing those invites the agent to
    spend a round on a file that can only fail."""
    (tmp_path / "executive_summary.md").write_text("x")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "report_without_citations.docx").write_bytes(b"PK\x03\x04\xff")
    (tmp_path / "charts").mkdir()
    (tmp_path / "charts" / "t-trend.png").write_bytes(b"\x89PNG")

    ctx = RunContextWrapper(AppContext(gateway=None, case_folder=tmp_path, logger=None))
    out = await fs_list_files.on_invoke_tool(ctx, "{}")

    assert "executive_summary.md" in out
    assert "notes.txt" in out
    assert "docx" not in out
    assert "charts" not in out and "png" not in out


def test_report_file_predicate_is_case_insensitive(tmp_path):
    """A `.MD` report is still a report. Detection already lowercased while
    the agent-facing lister did not, so such a case reported "has reports"
    and then handed report_agent an empty file list."""
    from tools.fs_tools import is_report_file
    upper = tmp_path / "SUMMARY.MD"
    upper.write_text("x")
    assert is_report_file(upper)


def test_detection_and_listing_agree_on_the_same_folder(tmp_path):
    """The contract that binds the three surfaces: if `_case_has_reports`
    says yes, the agent-facing list must be non-empty — otherwise report_agent
    is made mandatory and then given nothing to read."""
    from tools.fs_tools import is_report_file

    (tmp_path / "SUMMARY.MD").write_text("x")
    (tmp_path / "export.docx").write_bytes(b"PK\x03\x04\xff")

    # `_case_has_reports` resolves `_REPORTS_DIR / case_id` before scanning,
    # so rather than relocating the reports root we exercise the predicate it
    # now delegates to — which IS the shared contract under test.
    detected = any(is_report_file(p) for p in tmp_path.rglob("*"))
    listed = [p.name for p in tmp_path.iterdir() if is_report_file(p)]

    assert detected is True
    assert listed == ["SUMMARY.MD"]   # detection true ⇒ list non-empty
