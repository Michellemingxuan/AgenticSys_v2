"""Aggregate a completed blinded-review CSV and its provenance key.

Usage:

    python -m tests.test_consistency.score_reviews \
      results/evaluation_..._blind_review.csv \
      results/evaluation_..._review_key.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path


RATING_FIELDS = (
    "correctness_1_5",
    "completeness_1_5",
    "relevance_1_5",
    "clarity_1_5",
    "uncertainty_calibration_1_5",
)


def _rating(value: str, *, review_id: str, field: str) -> float | None:
    if not str(value or "").strip():
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{review_id}: {field} must be a number from 1 to 5") from exc
    if not 1 <= number <= 5:
        raise ValueError(f"{review_id}: {field}={number} is outside 1..5")
    return number


def aggregate_reviews(review_rows: list[dict], key_rows: list[dict]) -> dict:
    key = {row["review_id"]: row for row in key_rows}
    joined: list[dict] = []
    for review in review_rows:
        review_id = review.get("review_id", "")
        if review_id not in key:
            raise ValueError(f"{review_id}: missing from review key")
        ratings = {
            field: _rating(review.get(field, ""), review_id=review_id, field=field)
            for field in RATING_FIELDS
        }
        if not any(value is not None for value in ratings.values()):
            continue
        scope_raw = str(review.get("scope_correct_yes_no") or "").strip().lower()
        if scope_raw and scope_raw not in {"yes", "no", "y", "n"}:
            raise ValueError(
                f"{review_id}: scope_correct_yes_no must be yes/no or blank"
            )
        joined.append({
            **key[review_id],
            **ratings,
            "scope_correct": (
                scope_raw in {"yes", "y"} if scope_raw else None
            ),
            "has_unsupported_claims": bool(
                str(review.get("unsupported_claims") or "").strip()
            ),
        })

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in joined:
        grouped[(row.get("mode") or "?", row.get("name") or "?")].append(row)

    def summarize(rows: list[dict]) -> dict:
        out = {"n_reviewed": len(rows)}
        for field in RATING_FIELDS:
            values = [row[field] for row in rows if row[field] is not None]
            out[field] = statistics.mean(values) if values else None
        scope = [row["scope_correct"] for row in rows if row["scope_correct"] is not None]
        out["scope_correct_rate"] = (
            sum(scope) / len(scope) if scope else None
        )
        out["unsupported_claim_rate"] = (
            sum(row["has_unsupported_claims"] for row in rows) / len(rows)
            if rows else None
        )
        return out

    return {
        "n_reviewed": len(joined),
        "overall": summarize(joined),
        "questions": [
            {"mode": mode, "name": name, **summarize(rows)}
            for (mode, name), rows in sorted(grouped.items())
        ],
    }


def _markdown(summary: dict) -> str:
    def rating(value):
        return "—" if value is None else f"{value:.2f}"

    def pct(value):
        return "—" if value is None else f"{100 * value:.1f}%"

    lines = [
        "# Human content-quality review",
        "",
        f"Completed reviews: {summary['n_reviewed']}",
        "",
        "| Mode | Question | n | Correct | Complete | Relevant | Clear | "
        "Uncertainty | Scope correct | Unsupported claim |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["questions"]:
        lines.append(
            f"| {row['mode']} | {row['name']} | {row['n_reviewed']} | "
            f"{rating(row['correctness_1_5'])} | "
            f"{rating(row['completeness_1_5'])} | "
            f"{rating(row['relevance_1_5'])} | "
            f"{rating(row['clarity_1_5'])} | "
            f"{rating(row['uncertainty_calibration_1_5'])} | "
            f"{pct(row['scope_correct_rate'])} | "
            f"{pct(row['unsupported_claim_rate'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_csv", type=Path)
    parser.add_argument("review_key_csv", type=Path)
    parser.add_argument("--out-stem", type=Path, default=None)
    args = parser.parse_args()

    with args.review_csv.open(newline="", encoding="utf-8") as fh:
        reviews = list(csv.DictReader(fh))
    with args.review_key_csv.open(newline="", encoding="utf-8") as fh:
        keys = list(csv.DictReader(fh))
    summary = aggregate_reviews(reviews, keys)
    stem = args.out_stem or args.review_csv.with_name(
        args.review_csv.stem.replace("_blind_review", "") + "_human_quality"
    )
    json_path = Path(f"{stem}.json")
    markdown_path = Path(f"{stem}.md")
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(summary), encoding="utf-8")
    print(f"Reviewed: {summary['n_reviewed']}")
    print(f"Summary : {markdown_path}")


if __name__ == "__main__":
    main()

