"""Mask case-ID tokens (CASE-\\d+) before content flows to the LLM.

Used by SafeChainAdapter as a defense-in-depth layer: even if upstream
leaks (a new tool, an error string, a specialist's own output) contain
a raw case ID, the boundary scrubber masks it before the prompt reaches
the model.

Scope is intentionally narrow — one rule, nothing else. Digit masking,
role-label neutralization, and exec-keyword filtering live elsewhere in
SafeChainAdapter/FirewallStack.
"""

from __future__ import annotations

import re

_CASE_TOKEN = re.compile(r"\bCASE-\d+\b", flags=re.IGNORECASE)


def scrub(text: str) -> str:
    """Replace CASE-\\d+ tokens (case-insensitive) with the literal '<case>'.

    Idempotent: scrub(scrub(x)) == scrub(x) for all strings.
    """
    return _CASE_TOKEN.sub("<case>", text)
