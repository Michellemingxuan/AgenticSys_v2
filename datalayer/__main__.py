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

from datalayer.generator import DataGenerator

DEFAULT_N_CASES = 50


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate simulated data from YAML profiles")
    parser.add_argument("--output", default="data/simulated/", help="Output directory (default: data/simulated/)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--profile-dir", default="config/data_profiles", help="YAML profile directory")
    parser.add_argument("--cases", type=int, default=None,
                        help=f"Number of cases to generate (default: {DEFAULT_N_CASES}). "
                             "One-row-per-case tables get this many rows. "
                             "Multi-row tables scale proportionally.")
    parser.add_argument("--row-count", type=int, default=None,
                        help="Override row count for ALL tables (flat, no scaling). "
                             "Mutually exclusive with --cases.")
    args = parser.parse_args()

    if args.cases and args.row_count:
        parser.error("--cases and --row-count are mutually exclusive")

    cases = args.cases or DEFAULT_N_CASES
    gen = DataGenerator(profile_dir=args.profile_dir, seed=args.seed, cases=cases)
    gen.load_profiles()
    print(f"Loaded {len(gen.profiles)} profile(s) from {args.profile_dir}")
    print(f"  Generating {cases} cases")

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
