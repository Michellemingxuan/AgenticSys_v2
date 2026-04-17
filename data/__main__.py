"""CLI entry point: python -m data --output data/simulated/ --seed 42

Usage examples:
    # Default (50 cases, row counts from YAML profiles)
    python -m data --output data/simulated/ --seed 42

    # Generate 200 cases (multi-row tables scale proportionally)
    python -m data --output data/simulated/ --seed 42 --cases 200

    # Override all row counts to a flat number
    python -m data --output data/simulated/ --seed 42 --row-count 500
"""

from __future__ import annotations

import argparse

from data.generator import DataGenerator


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate simulated data from YAML profiles")
    parser.add_argument("--output", default="data/simulated/", help="Output directory (default: data/simulated/)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--profile-dir", default="config/data_profiles", help="YAML profile directory")
    parser.add_argument("--cases", type=int, default=None,
                        help="Number of cases to generate. One-row-per-case tables get this many rows. "
                             "Multi-row tables scale proportionally (e.g. 2x cases = 2x rows).")
    parser.add_argument("--row-count", type=int, default=None,
                        help="Override row count for ALL tables (flat, no scaling). "
                             "Mutually exclusive with --cases.")
    args = parser.parse_args()

    if args.cases and args.row_count:
        parser.error("--cases and --row-count are mutually exclusive")

    gen = DataGenerator(profile_dir=args.profile_dir, seed=args.seed)
    gen.load_profiles()
    print(f"Loaded {len(gen.profiles)} profile(s) from {args.profile_dir}")

    if args.cases:
        # Set one-row-per-case tables to the desired case count.
        # Multi-row tables using rows_per_case auto-scale via the generator.
        # Multi-row tables using row_count scale proportionally.
        baseline_cases = gen._get_case_count()

        for name, profile in gen.profiles.items():
            if profile.get("one_row_per_case", False):
                profile["row_count"] = args.cases
            elif "rows_per_case" not in profile:
                # Legacy row_count — scale proportionally
                scale_factor = args.cases / baseline_cases
                profile["row_count"] = max(1, int(profile["row_count"] * scale_factor))

        gen.generate_all()
        print(f"  Scaled to {args.cases} cases")
    else:
        gen.generate_all(row_count_override=args.row_count)

    paths = gen.dump_csv_per_case(args.output)

    case_ids = set()
    for p in paths:
        parts = p.split("/")
        if len(parts) >= 2:
            case_ids.add(parts[-2])

    print(f"  {len(case_ids)} cases, {len(paths)} files total")
    print("Done.")


if __name__ == "__main__":
    main()
