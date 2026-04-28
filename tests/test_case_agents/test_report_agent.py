"""Tests for agents.report_agent.ReportAgent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from case_agents.report_agent import ReportAgent
from logger.event_logger import EventLogger
from models.types import LLMResult, ReportDraft


@pytest.fixture
def logger(tmp_path):
    return EventLogger(session_id="test-report", log_dir=str(tmp_path))


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def case_folder_full(tmp_path):
    """A case folder with a few curated report files covering the question fully."""
    folder = tmp_path / "CASE-00001"
    folder.mkdir()
    (folder / "bureau.md").write_text(
        "# Bureau\nFICO score is 620. Two 30-day delinquencies in 2024."
    )
    (folder / "summary.md").write_text(
        "# Summary\nCustomer has acceptable credit bureau profile; moderate risk."
    )
    return folder


@pytest.fixture
def case_folder_partial(tmp_path):
    """A case folder with reports covering one topic but not another."""
    folder = tmp_path / "CASE-00002"
    folder.mkdir()
    (folder / "bureau.md").write_text("# Bureau\nFICO 580. Three derog marks.")
    return folder


@pytest.fixture
def empty_case_folder(tmp_path):
    folder = tmp_path / "CASE-EMPTY"
    folder.mkdir()
    return folder


async def test_run_empty_folder_returns_none_coverage(mock_llm, logger, empty_case_folder):
    """When the folder is empty, the agent short-circuits before any LLM call."""
    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("What is the bureau status?", empty_case_folder)

    assert isinstance(draft, ReportDraft)
    assert draft.coverage == "none"
    assert draft.files_consulted == []
    assert draft.answer == ""
    mock_llm.ainvoke.assert_not_called()


async def test_run_nonexistent_folder_returns_none_coverage(mock_llm, logger, tmp_path):
    """Missing folder is treated identically to empty folder."""
    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("What is the bureau status?", tmp_path / "does_not_exist")

    assert draft.coverage == "none"
    mock_llm.ainvoke.assert_not_called()


async def test_run_full_coverage(mock_llm, logger, case_folder_full):
    """Needle says full coverage, analysis returns an evidence-grounded answer."""
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            # Needle: picks both files, coverage=full
            LLMResult(
                status="success",
                data={
                    "relevant_files": ["bureau.md", "summary.md"],
                    "coverage": "full",
                    "hints": ["bureau profile", "overall summary"],
                },
            ),
            # Analysis: returns answer + excerpts
            LLMResult(
                status="success",
                data={
                    "answer": "The bureau profile is acceptable — FICO 620 with two 30-day delinquencies.",
                    "evidence_excerpts": [
                        '"FICO score is 620"',
                        '"Two 30-day delinquencies in 2024"',
                    ],
                },
            ),
        ]
    )

    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("What is the bureau status?", case_folder_full)

    assert draft.coverage == "full"
    assert "FICO 620" in draft.answer
    assert len(draft.evidence_excerpts) == 2
    assert draft.files_consulted == ["bureau.md", "summary.md"]
    assert mock_llm.ainvoke.call_count == 2


async def test_run_partial_coverage(mock_llm, logger, case_folder_partial):
    """Needle says partial coverage — only bureau.md is relevant; analysis still runs."""
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            LLMResult(
                status="success",
                data={
                    "relevant_files": ["bureau.md"],
                    "coverage": "partial",
                    "hints": ["bureau only, no capacity info"],
                },
            ),
            LLMResult(
                status="success",
                data={
                    "answer": "Bureau profile is weak (FICO 580, three derogs). Capacity not covered in reports.",
                    "evidence_excerpts": ['"FICO 580"', '"Three derog marks"'],
                },
            ),
        ]
    )

    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("What is the overall credit risk?", case_folder_partial)

    assert draft.coverage == "partial"
    assert "FICO 580" in draft.answer
    assert draft.files_consulted == ["bureau.md"]
    assert mock_llm.ainvoke.call_count == 2


async def test_run_needle_says_none_skips_analysis(mock_llm, logger, case_folder_full):
    """When Needle returns coverage=none, Analysis never runs."""
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(
            status="success",
            data={
                "relevant_files": [],
                "coverage": "none",
                "hints": [],
            },
        )
    )

    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("What's the weather?", case_folder_full)

    assert draft.coverage == "none"
    assert draft.files_consulted == []
    # Only the Needle call fired.
    assert mock_llm.ainvoke.call_count == 1


async def test_run_blocked_needle_returns_none_coverage(mock_llm, logger, case_folder_full):
    """Firewall block on the Needle step degrades to coverage=none."""
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(status="blocked", error="denied")
    )

    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("Anything?", case_folder_full)

    assert draft.coverage == "none"
    assert mock_llm.ainvoke.call_count == 1


async def test_run_coerces_invalid_coverage_to_none(mock_llm, logger, case_folder_full):
    """If the LLM returns an invalid coverage value, coerce to 'none' — never claim coverage on malformed output."""
    mock_llm.ainvoke = AsyncMock(
        return_value=LLMResult(
            status="success",
            data={
                "relevant_files": ["bureau.md"],
                "coverage": "maybe",  # not in {full, partial, none}
                "hints": [],
            },
        )
    )

    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("Q", case_folder_full)

    assert draft.coverage == "none"
    assert mock_llm.ainvoke.call_count == 1


async def test_run_filters_hallucinated_filenames(mock_llm, logger, case_folder_full):
    """If the Needle names a file that isn't in the folder, drop it silently."""
    mock_llm.ainvoke = AsyncMock(
        side_effect=[
            LLMResult(
                status="success",
                data={
                    "relevant_files": ["bureau.md", "nonexistent.md"],
                    "coverage": "full",
                    "hints": ["real", "fake"],
                },
            ),
            LLMResult(
                status="success",
                data={"answer": "A", "evidence_excerpts": ['"FICO score is 620"']},
            ),
        ]
    )

    agent = ReportAgent(mock_llm, logger)
    draft = await agent.run("Q", case_folder_full)

    assert draft.coverage == "full"
    assert draft.files_consulted == ["bureau.md"]
