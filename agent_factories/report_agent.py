"""Report Agent — SDK factory for scanning curated case reports.

The agent uses two markdown skills:
  - `workflow/report_needle.md` — coverage rubric (explicit | implicit | not_mentioned)
  - `workflow/report_analysis.md` — evidence extraction format

The agent calls fs_list_files, fs_grep, and fs_read_file tools autonomously
and returns a structured ReportDraft.
"""

from __future__ import annotations

from pathlib import Path

from agents import Agent, AgentOutputSchema, ModelSettings
from models.types import ReportDraft
from skills.loader import load_skill as _load_skill
from tools.fs_tools import fs_grep, fs_list_files, fs_read_file


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"

# Valid values the agent is expected to return for coverage.
_VALID_COVERAGES = {"explicit", "implicit", "not_mentioned"}

# ---------------------------------------------------------------------------
# SDK factory
# ---------------------------------------------------------------------------

# Compose instructions from the existing two-step prompts so the LLM has the
# same coverage rubric (explicit | implicit | not_mentioned) and evidence-
# extraction format. The agent decides on its own when to call fs_list_files,
# fs_grep, and fs_read_file.
_NEEDLE_PROMPT = _load_skill(_WORKFLOW_DIR / "report_needle.md").body
_ANALYSIS_PROMPT = _load_skill(_WORKFLOW_DIR / "report_analysis.md").body

REPORT_AGENT_INSTRUCTIONS = f"""\
You are the Report Agent. Your job is to scan a case folder for prior curated
reports (markdown files), locate the content relevant to the question, and
produce a ReportDraft that synthesizes AND analyzes what those reports say —
a qualitative risk read, not just a quote dump.

You have three tools: fs_list_files, fs_grep, fs_read_file.

This is a SYNTHESIS + ANALYSIS step over PRIOR CURATED REPORTS. Interpret and
reason over the content: connect findings across the report(s), draw
qualitative conclusions, and form an assessment of what the reports imply for
the question — grounded in direct quotes. The ONE hard boundary is numeric
integrity: you read curated report files only and have no data-table access,
so do NOT invent, recompute, or cross-check figures, or introduce any number
not stated in the reports — quantitative ground-truth is the domain
specialists' job; cite report numbers exactly as the reports state them. Be
efficient: locate, read the relevant slice(s), analyze, emit.

Workflow:
1. Your input includes a file list. First decide the layout:
   - **Several curated `<domain>_exp_0.md` files** → use the Coverage rubric's
     **Concept → file** table to pick the 1-2 most relevant files, then call
     `fs_read_file(filename="<chosen_file>")` on them (batch in ONE round, ≤ 2 files).
   - **One long consolidated file** (or files the table can't discriminate) →
     expand the question into search terms and call `fs_grep(terms=[...])`, then
     read a slice around the top matches with
     `fs_read_file(filename="<file>", start_line=<n>, end_line=<m>)`.
   The filenames in the list are ARGUMENTS to the read tools, NOT tool names.
2. Emit ReportDraft from what you read. Synthesize AND analyze — connect the
   load-bearing findings, interpret what they imply, and draw grounded
   conclusions about the question — but stay anchored: every claim traces to a
   quote in `evidence_excerpts`, and you introduce no number the reports don't
   state. Don't read more files "for context" once you have the relevant
   content.

If the folder is empty or no file is relevant, return
coverage="not_mentioned" with an empty answer and empty files_consulted.

# Output formatting (REQUIRED)

The `answer` field must be **bullet points**, not prose paragraphs. The
reasoning trace panel renders markdown — make it scannable. Format:

- Lead with the most load-bearing finding from the report(s).
- One bullet per discrete fact. Each bullet ≤ 2 sentences.
- **Bold the key terms / numbers / dates** so the reviewer's eye lands on
  them on first scan: `- **3 returned payments** in Dec-2024 totaling **$4,200**.`
- When the report covers multiple aspects of the question, group with
  short sub-headers: `### Spend trajectory` / `### Payment behavior`, etc.

`evidence_excerpts` stays as the verbatim-quotes list — those are the
direct backing for the bullets above.

=== Coverage rubric ===
{_NEEDLE_PROMPT}

=== Evidence extraction ===
{_ANALYSIS_PROMPT}
"""


def build_report_agent(model) -> Agent:
    return Agent(
        name="report_agent",
        instructions=REPORT_AGENT_INSTRUCTIONS,
        tools=[fs_grep, fs_list_files, fs_read_file],
        output_type=AgentOutputSchema(ReportDraft, strict_json_schema=False),
        model=model,
        # Report drafts are narrative + evidence excerpts — slightly more
        # room than the data specialists (1200) but still well below the
        # default. A typical ReportDraft is ~600-1500 tokens.
        model_settings=ModelSettings(max_tokens=2000, parallel_tool_calls=True),
    )
