"""Load prompt templates from config/prompts/{common,report,chat}.yaml.

Prompt assembly for specialists:
    common.yaml/common_format_instructions
    + {report|chat}.yaml/specialist
    + pillar.yaml/{domain}.report_instructions

Prompt assembly for synthesis:
    {report|chat}.yaml/synthesis
    + pillar.yaml/report_format
    + pillar.yaml/synthesis_instructions
"""

from __future__ import annotations

from pathlib import Path

import yaml

_cache: dict[str, dict] = {}


def _load_file(name: str, config_dir: str = "config") -> dict:
    if name in _cache:
        return _cache[name]

    path = Path(config_dir) / "prompts" / f"{name}.yaml"
    if not path.exists():
        path = Path(__file__).parent / "prompts" / f"{name}.yaml"
    if path.exists():
        with open(path) as f:
            _cache[name] = yaml.safe_load(f) or {}
    else:
        _cache[name] = {}
    return _cache[name]


def get_specialist_prompt(
    mode: str,
    question: str,
    findings: str,
    domain: str = "",
    pillar_report_instructions: str = "",
) -> str:
    """Assemble the specialist Step 3 prompt.

    = common_format_instructions + mode specialist + pillar report_instructions
    """
    common_data = _load_file("common")
    common = common_data.get("common_format_instructions", common_data.get("common_specialist", ""))

    mode_templates = _load_file(mode)  # "report" or "chat"
    mode_template = mode_templates.get("specialist", "Answer: $question\nFindings: $findings")

    mode_prompt = (mode_template
                   .replace("$question", question)
                   .replace("$findings", findings)
                   .replace("$domain", domain))

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

    = {report|chat}.yaml/synthesis
    + pillar report_format
    + pillar synthesis_report (instructions + per-section prompts + formatter)
    """
    mode_templates = _load_file(mode)
    base = mode_templates.get("synthesis", "Synthesize specialist outputs.")

    parts = [base]

    if pillar_report_format:
        parts.append(f"PILLAR REPORT FORMAT:\n{pillar_report_format}")

    # Handle synthesis_report as dict (structured) or string (flat)
    if isinstance(pillar_synthesis_report, dict):
        # Top-level instructions
        instructions = pillar_synthesis_report.get("instructions", "")
        if instructions:
            parts.append(f"PILLAR SYNTHESIS INSTRUCTIONS:\n{instructions}")

        # Per-section prompts (early_risk_prediction_section, etc.)
        for key, value in pillar_synthesis_report.items():
            if key in ("instructions", "formatter_report"):
                continue
            if isinstance(value, dict):
                section_instructions = value.get("report_instructions", "")
                if section_instructions:
                    parts.append(f"SECTION INSTRUCTIONS [{key}]:\n{section_instructions}")
            elif isinstance(value, str) and value.strip():
                parts.append(f"SECTION INSTRUCTIONS [{key}]:\n{value}")

        # Formatter (applied last)
        formatter = pillar_synthesis_report.get("formatter_report", "")
        if formatter:
            parts.append(f"FINAL FORMATTING INSTRUCTIONS:\n{formatter}")

    elif isinstance(pillar_synthesis_report, str) and pillar_synthesis_report.strip():
        parts.append(f"PILLAR SYNTHESIS INSTRUCTIONS:\n{pillar_synthesis_report}")

    return "\n\n".join(p for p in parts if p.strip())


def reload() -> None:
    """Force reload from disk (useful after editing templates)."""
    _cache.clear()
