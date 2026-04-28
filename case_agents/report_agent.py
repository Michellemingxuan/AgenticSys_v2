"""Report Agent — SDK factory for scanning curated case reports.

The agent uses two markdown skills:
  - `workflow/report_needle.md` — coverage rubric (full | partial | none)
  - `workflow/report_analysis.md` — evidence extraction format

The agent calls fs_list_files and fs_read_file tools autonomously and
returns a structured ReportDraft.
"""

from __future__ import annotations

from pathlib import Path

from agents import Agent
from models.types import ReportDraft
from skills.loader import load_skill as _load_skill
from tools.fs_tools import fs_list_files, fs_read_file


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"

# Valid values the agent is expected to return for coverage.
_VALID_COVERAGES = {"full", "partial", "none"}

# ---------------------------------------------------------------------------
# SDK factory
# ---------------------------------------------------------------------------

# Compose instructions from the existing two-step prompts so the LLM has the
# same coverage rubric (full | partial | none) and evidence-extraction format.
# The agent now decides on its own when to call fs_list_files and fs_read_file.
_NEEDLE_PROMPT = _load_skill(_WORKFLOW_DIR / "report_needle.md").body
_ANALYSIS_PROMPT = _load_skill(_WORKFLOW_DIR / "report_analysis.md").body

REPORT_AGENT_INSTRUCTIONS = f"""\
You are the Report Agent. Your job is to scan a case folder for prior curated
reports (markdown files), decide which are relevant to the question, read
them, and produce a ReportDraft.

You have two tools:
- fs_list_files() — list files in the case folder
- fs_read_file(filename) — read a named file

Workflow:
1. Call fs_list_files to see what's available.
2. Decide coverage and which files are relevant per the rubric below.
3. Read the relevant files via fs_read_file.
4. Produce a ReportDraft with: answer (synthesized), coverage (full | partial
   | none), evidence_excerpts (verbatim quotes), files_consulted (list of
   filenames you actually read).

If the folder is empty or no file is relevant, return coverage="none" with an
empty answer and empty files_consulted.

=== Coverage rubric ===
{_NEEDLE_PROMPT}

=== Evidence extraction ===
{_ANALYSIS_PROMPT}
"""


def build_report_agent(model) -> Agent:
    return Agent(
        name="report_agent",
        instructions=REPORT_AGENT_INSTRUCTIONS,
        tools=[fs_list_files, fs_read_file],
        output_type=ReportDraft,
        model=model,
    )
