"""CLI entry point: python -m data --output data/simulated/ --seed 42"""

from __future__ import annotations

import argparse
import sys

from data.generator import DataGenerator


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate simulated data from YAML profiles")
    parser.add_argument("--output", default="data/simulated/", help="Output directory for CSVs")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--profile-dir", default="config/data_profiles", help="YAML profile directory")
    parser.add_argument("--row-count", type=int, default=None, help="Override row count for all tables")
    args = parser.parse_args()

    gen = DataGenerator(profile_dir=args.profile_dir, seed=args.seed)
    gen.load_profiles()
    print(f"Loaded {len(gen.profiles)} profile(s) from {args.profile_dir}")

    gen.generate_all(row_count_override=args.row_count)
    paths = gen.dump_csv(args.output)

    for p in paths:
        print(f"  wrote {p}")
    print("Done.")


if __name__ == "__main__":
    main()
