"""Mask the active case-ID literal before content flows to the LLM.

Used by SafeChainAdapter as a defense-in-depth layer: even if upstream
leaks (a new tool, an error string, a specialist's own output) contain
the raw case ID, the boundary scrubber replaces it with the neutral
<case> token before the prompt reaches the model.

Scope is intentionally narrow — one rule. The scrubber needs the
current case ID because the production format is an 11-digit number
with no visible prefix, making it indistinguishable from other digit
runs without context. Digit masking, role-label neutralization, and
exec-keyword filtering live elsewhere in SafeChainAdapter/FirewallStack.
"""

from __future__ import annotations

import re


def scrub(text: str, case_id: str | None) -> str:
    """Replace the ``case_id`` literal with ``<case>`` at word boundaries.

    When ``case_id`` is None or empty, returns ``text`` unchanged — no
    active case means nothing to scrub. Idempotent for any given case_id.
    """
    if not case_id:
        return text
    pattern = rf"\b{re.escape(case_id)}\b"
    return re.sub(pattern, "<case>", text)
