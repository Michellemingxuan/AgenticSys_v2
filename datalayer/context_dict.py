"""Parse context-dictionary txt files (context/*_context_description.txt).

Each substantive line is `N. var_name: description. threshold sentence`.
Pure-Python (no pandas). Threshold *interpretation* lives in threshold.py;
this module only splits the description from the threshold sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# "1. var_name: rest"  — leading index optional, var_name is snake/alnum.
_LINE = re.compile(r"^\s*\d+\.\s*([A-Za-z0-9_]+)\s*:\s*(.+)$")
# A sentence that states a risk threshold.
_THRESHOLD_SENTENCE = re.compile(
    r"\b(?:values?|scores?)\b.*?\b(?:risky|risk)\b[^.]*\.",
    re.IGNORECASE,
)


@dataclass
class ContextEntry:
    var_name: str
    raw_description: str
    threshold_text: str | None


def parse_context_file(path: str) -> list[ContextEntry]:
    entries: list[ContextEntry] = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            m = _LINE.match(line)
            if not m:
                continue
            var_name, rest = m.group(1), m.group(2).strip()
            tm = _THRESHOLD_SENTENCE.search(rest)
            threshold_text = tm.group(0).strip() if tm else None
            description = rest.replace(threshold_text, "").strip() if threshold_text else rest
            entries.append(ContextEntry(var_name, description, threshold_text))
    return entries
