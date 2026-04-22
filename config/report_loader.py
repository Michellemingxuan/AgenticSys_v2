"""Load prompt templates from config/prompts/prompts.yaml.

Single-file source of truth. Top-level keys: ``common``, ``chat``, ``report``.

Prompt assembly for specialists (``get_specialist_prompt``):
    common.format_instructions
    + {chat|report}.specialist        ($question, $findings, $domain substituted)
    + pillar.{domain}.report_instructions

Prompt assembly for synthesis (``get_synthesis_prompt``):
    {chat|report}.synthesis
    + pillar.report_format
    + pillar.synthesis_report (instructions + per-section prompts + formatter)
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PROMPTS_FILE = "prompts.yaml"
_cache: dict | None = None


def _load_prompts(config_dir: str = "config") -> dict:
    """Load prompts.yaml once and cache. Falls back to package-relative path."""
    global _cache
    if _cache is not None:
        return _cache

    path = Path(config_dir) / "prompts" / _PROMPTS_FILE
    if not path.exists():
        path = Path(__file__).parent / "prompts" / _PROMPTS_FILE

    if path.exists():
        with open(path) as f:
            _cache = yaml.safe_load(f) or {}
    else:
        _cache = {}

    return _cache


def get_specialist_prompt(
    mode: str,
    question: str,
    findings: str,
    domain: str = "",
    pillar_report_instructions: str = "",
) -> str:
    """Assemble the specialist Step-3 prompt.

    common.format_instructions + {mode}.specialist + pillar.report_instructions
    """
    data = _load_prompts()
    common = data.get("common", {}).get("format_instructions", "")

    mode_template = (
        data.get(mode, {}).get("specialist")
        or "Answer: $question\nFindings: $findings"
    )
    mode_prompt = (
        mode_template
        .replace("$question", question)
        .replace("$findings", findings)
        .replace("$domain", domain)
    )

    parts = [common, mode_prompt]
    if pillar_report_instructions:
        parts.append(f"PILLAR-SPECIFIC INSTRUCTIONS:\n{pillar_report_instructions}")

    return "\n\n".join(p for p in parts if p.strip())


def get_synthesis_prompt(
    mode: str,
    pillar_report_format: str = "",
    pillar_synthesis_report: dict | str = "",
) -> str:
    """Assemble the orchestrator synthesis prompt.

    {mode}.synthesis + pillar.report_format + pillar.synthesis_report
    """
    data = _load_prompts()
    base = data.get(mode, {}).get("synthesis") or "Synthesize specialist outputs."

    parts: list[str] = [base]

    if pillar_report_format:
        parts.append(f"PILLAR REPORT FORMAT:\n{pillar_report_format}")

    # synthesis_report may be a flat string or a structured dict.
    if isinstance(pillar_synthesis_report, dict):
        instructions = pillar_synthesis_report.get("instructions", "")
        if instructions:
            parts.append(f"PILLAR SYNTHESIS INSTRUCTIONS:\n{instructions}")

        for key, value in pillar_synthesis_report.items():
            if key in ("instructions", "formatter_report"):
                continue
            if isinstance(value, dict):
                section_instructions = value.get("report_instructions", "")
                if section_instructions:
                    parts.append(f"SECTION INSTRUCTIONS [{key}]:\n{section_instructions}")
            elif isinstance(value, str) and value.strip():
                parts.append(f"SECTION INSTRUCTIONS [{key}]:\n{value}")

        formatter = pillar_synthesis_report.get("formatter_report", "")
        if formatter:
            parts.append(f"FINAL FORMATTING INSTRUCTIONS:\n{formatter}")

    elif isinstance(pillar_synthesis_report, str) and pillar_synthesis_report.strip():
        parts.append(f"PILLAR SYNTHESIS INSTRUCTIONS:\n{pillar_synthesis_report}")

    return "\n\n".join(p for p in parts if p.strip())


def reload() -> None:
    """Force reload from disk (useful after editing prompts.yaml)."""
    global _cache
    _cache = None
