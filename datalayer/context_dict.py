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
# The pattern allows dots only between digits (e.g. 5.8) so it cannot bleed
# across a sentence boundary when an earlier keyword appears in the description.
_THRESHOLD_SENTENCE = re.compile(
    r"\b(?:values?|scores?)\b(?:[^.]|\d\.\d)*\b(?:risky|risk)\b[^.]*\.",
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


_RANGE = re.compile(r"\bfrom\s+(-?\d+(?:\.\d+)?)\s*[-to]+\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_BOUND = re.compile(r"\b(above|below|over|under|greater than|less than|on or above|on or below)\b\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)

_BELOW_WORDS = {"below", "under", "less than", "on or below"}


def normalize_threshold(text: str | None) -> dict | None:
    if not text:
        return None
    rm = _RANGE.search(text)
    if rm:
        return {"risk_threshold": [float(rm.group(1)), float(rm.group(2))], "risk_direction": "range"}
    bm = _BOUND.search(text)
    if bm:
        word = bm.group(1).lower()
        direction = "below" if word in _BELOW_WORDS else "above"
        return {"risk_threshold": float(bm.group(2)), "risk_direction": direction}
    return None
