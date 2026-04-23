"""Report Agent — scans `reports/<case-id>/*.md` for curated reports and
extracts an evidence-grounded answer to a reviewer's question.

The agent runs a two-step chain over its markdown skills:
  1. `workflow/report_needle.md` — decide which files are relevant and how
     well they cover the question (coverage = full | partial | none).
  2. `workflow/report_analysis.md` — read the selected files and produce a
     ReportDraft with answer + verbatim evidence excerpts.

Coverage = "none" short-circuits before Step 2 — nothing to read.
"""

from __future__ import annotations

from pathlib import Path

from gateway.firewall_stack import FirewalledModel
from logger.event_logger import EventLogger
from models.types import ReportDraft
from skills.loader import load_skill as _load_skill


_WORKFLOW_DIR = Path(__file__).parent.parent / "skills" / "workflow"

# Valid values the Needle is allowed to return. Anything else is coerced
# to "none" so a malformed LLM response can never silently claim coverage.
_VALID_COVERAGES = {"full", "partial", "none"}


class ReportAgent:
    """Consults curated case reports in parallel with the team workflow.

    The agent is stateless across questions: each `run()` call re-scans the
    case folder. That keeps it safe when the folder changes mid-session
    (e.g., the user drops in a new report between questions).
    """

    def __init__(self, llm: FirewalledModel, logger: EventLogger):
        self.llm = llm
        self.logger = logger
        self._needle_prompt = _load_skill(_WORKFLOW_DIR / "report_needle.md").body
        self._analysis_prompt = _load_skill(_WORKFLOW_DIR / "report_analysis.md").body

    async def run(self, question: str, case_folder: Path) -> ReportDraft:
        """Scan the case folder and return a ReportDraft.

        Returns `coverage="none"` when the folder is missing, empty, or the
        Needle decides nothing is relevant.
        """
        case_folder = Path(case_folder)
        files = sorted(case_folder.glob("*.md")) if case_folder.exists() else []

        self.logger.log(
            "report_needle_start",
            {"question": question, "case_folder": str(case_folder), "file_count": len(files)},
        )

        if not files:
            self.logger.log("report_needle_empty", {"case_folder": str(case_folder)})
            return ReportDraft(coverage="none")

        # Step 1: Needle — let the LLM pick relevant files and decide coverage.
        file_list_str = "\n".join(f"- {p.name}" for p in files)
        needle_result = await self.llm.ainvoke(
            system_prompt=self._needle_prompt,
            user_message=(
                f"Question: {question}\n\n"
                f"Available files in the case folder:\n{file_list_str}\n\n"
                "Decide which files are relevant and judge coverage."
            ),
        )

        if needle_result.status == "blocked" or needle_result.data is None:
            self.logger.log(
                "report_needle_fallback",
                {"reason": "blocked — defaulting to coverage=none"},
            )
            return ReportDraft(coverage="none")

        needle_data = needle_result.data
        coverage = needle_data.get("coverage", "none")
        if coverage not in _VALID_COVERAGES:
            coverage = "none"

        relevant = needle_data.get("relevant_files", []) or []
        if not isinstance(relevant, list):
            relevant = []

        # Only keep files the Needle named that actually exist on disk.
        file_names = {p.name for p in files}
        selected = [p for p in files if p.name in relevant and p.name in file_names]
        names_selected = [p.name for p in selected]

        self.logger.log(
            "report_needle_done",
            {"coverage": coverage, "selected": names_selected},
        )

        if coverage == "none" or not selected:
            return ReportDraft(coverage="none", files_consulted=[])

        # Step 2: Analysis — read the selected reports and produce an answer.
        report_text = "\n\n".join(
            f"=== {p.name} ===\n{p.read_text(encoding='utf-8')}" for p in selected
        )
        analysis_result = await self.llm.ainvoke(
            system_prompt=self._analysis_prompt,
            user_message=(
                f"Question: {question}\n\n"
                f"Curated report content:\n{report_text}\n\n"
                "Extract an evidence-grounded answer."
            ),
        )

        if analysis_result.status == "blocked" or analysis_result.data is None:
            self.logger.log(
                "report_analysis_fallback",
                {"reason": "blocked — returning needle-only draft"},
            )
            return ReportDraft(coverage=coverage, files_consulted=names_selected)

        analysis_data = analysis_result.data
        answer = str(analysis_data.get("answer", ""))
        excerpts = analysis_data.get("evidence_excerpts", []) or []
        if not isinstance(excerpts, list):
            excerpts = []

        self.logger.log(
            "report_analysis_done",
            {"answer_len": len(answer), "excerpt_count": len(excerpts)},
        )

        return ReportDraft(
            coverage=coverage,
            answer=answer,
            evidence_excerpts=[str(x) for x in excerpts],
            files_consulted=names_selected,
        )
