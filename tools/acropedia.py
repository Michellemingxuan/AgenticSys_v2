"""Acropedia abbreviation-lookup tool (stub adapter).

Swappable for a real internal-platform client later — callers depend only
on the (`term`) → `{full_name, explanation}` contract documented in
`skills/helper/acropedia.md`. Keep the canned entries short and
domain-focused so agents get useful results during local dev and tests.
"""

from __future__ import annotations


# Canned entries for terms the system commonly encounters. Keyed by the
# case-folded term; lookup is case-insensitive.
_ENTRIES: dict[str, dict[str, str]] = {
    "dti": {
        "full_name": "Debt-To-Income Ratio",
        "explanation": (
            "A ratio of monthly debt payments to gross monthly income. "
            "A common regulatory benchmark is 0.43 (43%); above that is a stress signal."
        ),
    },
    "fico": {
        "full_name": "Fair Isaac Corporation Score",
        "explanation": (
            "A three-digit credit score (300-850) summarizing bureau credit history. "
            "Higher is better. Common cut-offs: < 580 subprime, 670-739 near-prime, 740+ prime."
        ),
    },
    "wcc": {
        "full_name": "Watch-List / Compliance Control",
        "explanation": (
            "Screening against sanctions, PEP, and adverse-media lists. "
            "An active WCC flag requires enhanced due diligence before any risk decision."
        ),
    },
    "cbr": {
        "full_name": "Consumer Bureau Risk",
        "explanation": (
            "Internal risk score derived from bureau data. Compare to the model-side "
            "credit_loss_prob to detect divergence between external and internal risk views."
        ),
    },
    "pd": {
        "full_name": "Probability of Default",
        "explanation": (
            "Model-estimated probability that the obligor defaults within a given horizon. "
            "Expressed as a decimal (0.00-1.00)."
        ),
    },
}


def acropedia_lookup(term: str) -> dict:
    """Look up ``term`` in Acropedia. Case-insensitive.

    Returns a dict with ``full_name`` and ``explanation`` keys. For unknown
    terms, the dict carries the input term as ``full_name`` and a short
    "not available" note in ``explanation`` — never hallucinate a definition.
    """
    key = (term or "").strip().lower()
    entry = _ENTRIES.get(key)
    if entry is None:
        return {
            "full_name": term,
            "explanation": "Definition not available in Acropedia stub",
        }
    # Return a copy so callers can't mutate the shared dict.
    return dict(entry)
