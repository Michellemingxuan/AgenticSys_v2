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
produce a ReportDraft that synthesizes a concise qualitative narrative from
what those reports say.

You have three tools: fs_list_files, fs_grep, fs_read_file.

This is a LIGHT SYNTHESIS step over PRIOR CURATED REPORTS — not fresh analysis.
You may connect and summarize the descriptive findings across the report(s)
into a coherent qualitative narrative, grounded in direct quotes. You do NOT
compute new numbers, recompute or cross-check figures, or introduce any number
that isn't stated in the reports — quantitative work belongs to the domain
specialists. Be fast: locate, read the relevant slice(s), synthesize, emit.

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
2. Emit ReportDraft from what you read. You MAY synthesize the report(s)'
   descriptive findings into a coherent qualitative overview — summarize and
   connect the load-bearing points — but stay grounded: every claim traces to
   a quote in `evidence_excerpts`, and you introduce no number the reports
   don't state. Don't read more files "for context" once you have the relevant
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
