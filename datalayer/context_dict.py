"""Parse context-dictionary txt files (context/*_context_description.txt).

Each substantive line is `N. var_name: description. threshold sentence`.
Pure-Python (no pandas). Threshold *interpretation* lives in threshold.py;
this module only splits the description from the threshold sentence.
"""
from __future__ import annotations

import os
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
    threshold: dict | None = None


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


# Static domain → tables map (filename stem before "_context_description.txt").
CONTEXT_TABLE_MAP: dict[str, list[str]] = {
    "modeling": ["model_scores", "model_scores_transaction"],
    "score_driver": ["score_drivers", "score_drivers_transaction"],
    "crossbu": ["crossbu_cards", "crossbu_merchants"],
    "spend": ["spends"],
    "payment": ["payments"],
    "payment_spend": ["spends", "payments"],
    "bureau": ["bureau"],
    "strategy": ["strategy"],
}


def load_context_by_table(context_dir: str) -> dict[str, dict[str, ContextEntry]]:
    """Load context entries grouped by canonical table name.

    Returns a nested dict: {canonical_table: {var_name: ContextEntry}}.
    Each ContextEntry.threshold is pre-normalized via normalize_threshold.
    Returns an empty dict when *context_dir* does not exist (clean degradation
    for fresh checkouts where context/ is gitignored).
    """
    if not os.path.isdir(context_dir):
        return {}
    out: dict[str, dict[str, ContextEntry]] = {}
    for fname in sorted(os.listdir(context_dir)):
        if not fname.endswith("_context_description.txt"):
            continue
        stem = fname[: -len("_context_description.txt")]
        tables = CONTEXT_TABLE_MAP.get(stem)
        if not tables:
            continue
        entries = parse_context_file(os.path.join(context_dir, fname))
        for e in entries:
            e.threshold = normalize_threshold(e.threshold_text)
        for table in tables:
            bucket = out.setdefault(table, {})
            for e in entries:
                bucket[e.var_name] = e
    return out


def _fmt(n: float) -> str:
    """Format a float: integer-valued floats render without .0."""
    return str(int(n)) if float(n).is_integer() else str(n)


def render_threshold(threshold: dict | None) -> str:
    """Inverse of normalize_threshold: structured threshold dict → sentence.

    Returns a risk threshold sentence like "Values above 5.8 are risky."
    or "Scores from 10 to 100 are risky." for a structured threshold dict,
    or "" for None/empty.
    """
    if not threshold:
        return ""
    direction = threshold.get("risk_direction")
    value = threshold.get("risk_threshold")
    if direction == "range" and isinstance(value, (list, tuple)) and len(value) == 2:
        return f"Scores from {_fmt(value[0])} to {_fmt(value[1])} are risky."
    if direction in ("above", "below") and value is not None:
        return f"Values {direction} {_fmt(value)} are risky."
    return ""


def context_files_for_table(table: str) -> list[str]:
    """Return context-file stems whose CONTEXT_TABLE_MAP entry includes *table*."""
    return [stem for stem, tables in CONTEXT_TABLE_MAP.items() if table in tables]


def update_context_entry(
    context_dir: str,
    table: str,
    var_name: str,
    description: str,
    threshold: dict | None,
) -> str:
    """Rewrite the existing line for *var_name* in the table's single context file.

    Returns one of:
      "updated"       — rewrote the var's line; all other lines preserved byte-for-byte.
      "not_found"     — single file exists but has no line for *var_name*.
      "multi_context" — table is covered by >1 context file; nothing written.
      "no_context"    — table maps to 0 context files; nothing written.
    """
    stems = context_files_for_table(table)
    if not stems:
        return "no_context"
    if len(stems) > 1:
        return "multi_context"
    path = os.path.join(context_dir, f"{stems[0]}_context_description.txt")
    if not os.path.isfile(path):
        return "not_found"
    sentence = render_threshold(threshold)
    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    out, found = [], False
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) == var_name:
            idx = line.split(".", 1)[0].strip()
            desc = description.strip().rstrip(".")
            body = f"{desc}. {sentence}" if sentence else desc
            out.append(f"{idx}. {var_name}: {body}\n")
            found = True
        else:
            out.append(line)
    if not found:
        return "not_found"
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)
    return "updated"
