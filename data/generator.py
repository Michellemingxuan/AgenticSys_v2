"""Simulated data generator driven by YAML profile configs."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

import numpy as np
import yaml

CASE_ID_COLUMN = "case_id"
CASE_ID_FORMAT = "CASE-{seq:05d}"


class DataGenerator:
    """Generates simulated tabular data from YAML profile definitions.

    Each profile specifies columns with distribution parameters, row counts,
    and optional rank-based correlations.
    """

    def __init__(self, profile_dir: str = "config/data_profiles", seed: int = 42,
                 cases: int = 50):
        self.profile_dir = Path(profile_dir)
        self.seed = seed
        self.cases = cases         # number of cases to generate
        self.profiles: dict[str, dict] = {}
        self._tables: dict[str, dict[str, list]] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_profiles(self) -> None:
        """Load all YAML profiles from the profile directory."""
        self.profiles.clear()
        for path in sorted(self.profile_dir.glob("*.yaml")):
            with open(path) as f:
                profile = yaml.safe_load(f)
            self.profiles[profile["table"]] = profile

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate_all(self, row_count_override: int | None = None) -> dict[str, dict[str, list]]:
        """Generate all tables, returning {table_name: {col_name: [values]}}."""
        self._tables.clear()
        for name, profile in self.profiles.items():
            self._tables[name] = self._generate_table(profile, row_count_override)
        return self._tables

    def _generate_table(
        self, profile: dict, row_count_override: int | None = None
    ) -> dict[str, list]:
        if row_count_override is not None:
            n = row_count_override
        elif "rows_per_case" in profile:
            # rows_per_case × number of cases = total rows
            n = profile["rows_per_case"] * self._get_case_count()
        elif profile.get("one_row_per_case", False):
            # Convenience: one_row_per_case without an explicit rows_per_case → 1 row per case
            n = self._get_case_count()
        else:
            n = profile["row_count"]
        rng = np.random.default_rng(self.seed)

        columns: dict[str, list] = {}
        col_specs = profile["columns"]

        # First pass: generate non-derived columns
        derived_specs: dict[str, dict] = {}
        for col_name, spec in col_specs.items():
            if "derive_from" in spec:
                derived_specs[col_name] = spec
                continue
            columns[col_name] = self._generate_column(spec, n, rng, profile)

        # Apply correlations (only on base columns)
        correlations = profile.get("correlations", [])
        if correlations:
            self._apply_correlations(columns, correlations, col_specs, n, rng)

        # Second pass: derived columns, computed from already-generated source columns
        for col_name, spec in derived_specs.items():
            columns[col_name] = self._derive_column(spec, columns, n)

        # Inject case_id column as generator infrastructure (idempotent — skip if profile still declares it).
        if CASE_ID_COLUMN not in columns:
            one_row = profile.get("one_row_per_case", False)
            case_count = self._get_case_count()
            if one_row:
                columns[CASE_ID_COLUMN] = [CASE_ID_FORMAT.format(seq=i + 1) for i in range(n)]
            else:
                columns[CASE_ID_COLUMN] = [CASE_ID_FORMAT.format(seq=(i % case_count) + 1) for i in range(n)]

        return columns

    def _derive_column(self, spec: dict, columns: dict, n: int) -> list:
        """Compute a column from another column using a transform."""
        source = spec["derive_from"]
        transform = spec.get("transform", "identity")
        if source not in columns:
            raise ValueError(f"derive_from source '{source}' not found in columns")
        source_values = columns[source]

        if transform == "month_name":
            # Convert YYYY-MM-DD (or date string) to month name (e.g. "October")
            from datetime import date
            months = [
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ]
            result = []
            for v in source_values:
                try:
                    if isinstance(v, str):
                        d = date.fromisoformat(v)
                    else:
                        d = v  # already a date
                    result.append(months[d.month - 1])
                except Exception:
                    result.append("")
            return result

        if transform == "month_year":
            # e.g. "October'2024"
            from datetime import date
            months = [
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ]
            result = []
            for v in source_values:
                try:
                    d = date.fromisoformat(v) if isinstance(v, str) else v
                    result.append(f"{months[d.month - 1]}'{d.year}")
                except Exception:
                    result.append("")
            return result

        if transform == "identity":
            return list(source_values)

        raise ValueError(f"Unknown transform: {transform}")

    def _generate_column(
        self, spec: dict, n: int, rng: np.random.Generator, profile: dict
    ) -> list:
        dtype = spec["dtype"]

        if dtype == "string":
            return self._gen_string(spec, n, profile)
        elif dtype == "int":
            return self._gen_int(spec, n, rng)
        elif dtype == "float":
            return self._gen_float(spec, n, rng)
        elif dtype == "categorical":
            return self._gen_categorical(spec, n, rng)
        elif dtype == "date":
            return self._gen_date(spec, n, rng)
        else:
            raise ValueError(f"Unknown dtype: {dtype}")

    def _get_case_count(self) -> int:
        """Return the number of cases to generate."""
        return self.cases

    def _gen_string(self, spec: dict, n: int, profile: dict) -> list:
        fmt = spec["format"]
        one_row = profile.get("one_row_per_case", False)
        if one_row:
            return [fmt.format(seq=i + 1) for i in range(n)]
        else:
            case_count = self._get_case_count()
            return [fmt.format(seq=(i % case_count) + 1) for i in range(n)]

    def _gen_int(self, spec: dict, n: int, rng: np.random.Generator) -> list:
        dist = spec.get("distribution", "uniform")
        lo = spec.get("min", 0)
        hi = spec.get("max", 100)

        if dist == "normal":
            mean = spec["mean"]
            std = spec["std"]
            values = rng.normal(mean, std, n)
            values = np.clip(np.round(values), lo, hi).astype(int)
        elif dist == "poisson":
            lam = spec["lambda"]
            values = rng.poisson(lam, n)
            values = np.clip(values, lo, hi).astype(int)
        elif dist == "uniform":
            values = rng.integers(lo, hi + 1, size=n)
        else:
            raise ValueError(f"Unknown int distribution: {dist}")

        return values.tolist()

    def _gen_float(self, spec: dict, n: int, rng: np.random.Generator) -> list:
        dist = spec.get("distribution", "uniform")
        lo = spec.get("min", 0.0)
        hi = spec.get("max", 1.0)

        if dist == "normal":
            mean = spec["mean"]
            std = spec["std"]
            values = rng.normal(mean, std, n)
            values = np.clip(values, lo, hi)
        elif dist == "uniform":
            values = rng.uniform(lo, hi, n)
        else:
            raise ValueError(f"Unknown float distribution: {dist}")

        return [round(float(v), 4) for v in values]

    def _gen_categorical(self, spec: dict, n: int, rng: np.random.Generator) -> list:
        cats = spec["categories"]
        labels = list(cats.keys())
        probs = np.array([cats[k] for k in labels], dtype=float)
        probs /= probs.sum()  # normalize
        indices = rng.choice(len(labels), size=n, p=probs)
        return [labels[i] for i in indices]

    def _gen_date(self, spec: dict, n: int, rng: np.random.Generator) -> list:
        year = spec.get("year", 2024)
        if isinstance(year, list):
            # span multiple years
            start_year = min(year)
            end_year = max(year)
        else:
            start_year = year
            end_year = year

        # Generate random dates within the year range
        start_ord = _date_to_ordinal(start_year, 1, 1)
        end_ord = _date_to_ordinal(end_year, 12, 31)
        ordinals = rng.integers(start_ord, end_ord + 1, size=n)
        return [_ordinal_to_date_str(int(o)) for o in ordinals]

    # ------------------------------------------------------------------
    # Rank-based correlation
    # ------------------------------------------------------------------

    def _apply_correlations(
        self,
        columns: dict[str, list],
        correlations: list[dict],
        col_specs: dict,
        n: int,
        rng: np.random.Generator,
    ) -> None:
        """Apply rank-based correlations between numeric column pairs."""
        for corr in correlations:
            col_a, col_b = corr["columns"]
            direction = corr["direction"]
            # strength is informational; we apply a full rank-sort

            if col_a not in columns or col_b not in columns:
                continue

            vals_a = np.array(columns[col_a], dtype=float)
            vals_b = np.array(columns[col_b], dtype=float)

            # Sort both arrays by rank
            order_a = np.argsort(vals_a)
            sorted_b = np.sort(vals_b)

            if direction == "negative":
                sorted_b = sorted_b[::-1]

            # Assign sorted_b values in the order determined by col_a's ranks
            new_b = np.empty_like(vals_b)
            new_b[order_a] = sorted_b

            # Clip to spec bounds
            spec_b = col_specs[col_b]
            lo = spec_b.get("min", None)
            hi = spec_b.get("max", None)
            if lo is not None or hi is not None:
                new_b = np.clip(new_b, lo, hi)

            # Convert back
            if spec_b["dtype"] == "int":
                columns[col_b] = [int(round(v)) for v in new_b]
            else:
                columns[col_b] = [round(float(v), 4) for v in new_b]

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def dump_csv(self, output_dir: str) -> list[str]:
        """Write all generated tables to CSV files (flat layout). Returns list of file paths."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for table_name, cols in self._tables.items():
            path = out / f"{table_name}.csv"
            col_names = list(cols.keys())
            n = len(next(iter(cols.values())))
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(col_names)
                for i in range(n):
                    writer.writerow([cols[c][i] for c in col_names])
            paths.append(str(path))
        return paths

    def dump_csv_per_case(self, output_dir: str) -> list[str]:
        """Write tables organized by case folder: output_dir/{case_id}/{table}.csv.

        Each case folder contains only the rows belonging to that case.
        For one_row_per_case tables: the single row (without case_id column).
        For multi-row tables: all rows matching that case_id (without case_id column).
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # Collect all case IDs from any table that has a case_id column
        case_ids: set[str] = set()
        for cols in self._tables.values():
            if "case_id" in cols:
                case_ids.update(cols["case_id"])

        paths = []
        for case_id in sorted(case_ids):
            case_dir = out / case_id
            case_dir.mkdir(parents=True, exist_ok=True)

            for table_name, cols in self._tables.items():
                col_names = list(cols.keys())
                n = len(next(iter(cols.values())))

                if "case_id" not in cols:
                    continue

                # Find row indices for this case
                indices = [i for i in range(n) if cols["case_id"][i] == case_id]
                if not indices:
                    continue

                # Write CSV without case_id column (it's implicit from the folder)
                data_cols = [c for c in col_names if c != "case_id"]
                path = case_dir / f"{table_name}.csv"
                with open(path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(data_cols)
                    for i in indices:
                        writer.writerow([cols[c][i] for c in data_cols])
                paths.append(str(path))

        return paths


# ------------------------------------------------------------------
# Date helpers (no dependency on datetime for deterministic ordinal math)
# ------------------------------------------------------------------

def _is_leap(y: int) -> bool:
    return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)


def _days_in_year(y: int) -> int:
    return 366 if _is_leap(y) else 365


def _date_to_ordinal(y: int, m: int, d: int) -> int:
    from datetime import date
    return date(y, m, d).toordinal()


def _ordinal_to_date_str(ordinal: int) -> str:
    from datetime import date
    dt = date.fromordinal(ordinal)
    return dt.isoformat()
