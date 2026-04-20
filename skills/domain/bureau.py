"""Bureau domain skill — tradeline analysis, derog marks, score interpretation."""

from models.types import DomainSkill


def get_skill() -> DomainSkill:
    return DomainSkill(
        name="bureau",
        system_prompt=(
            "You are a bureau-data credit analyst. You specialise in tradeline analysis, "
            "derogatory marks, inquiry patterns, and credit-score interpretation. "
            "Interpret bureau data in the context of credit risk, highlighting score drivers, "
            "derog severity, and tradeline age/mix."
        ),
        data_hints=["bureau"],
        interpretation_guide=(
            "High derog counts with low scores are expected; flag cases where score is "
            "surprisingly high despite derogs. Inquiry spikes may signal credit-seeking behaviour."
        ),
        risk_signals=[
            "score below 600",
            "derog_count >= 3",
            "inquiry spike (>5 in 6 months)",
            "thin file (tradeline_ct < 3)",
        ],
    )
