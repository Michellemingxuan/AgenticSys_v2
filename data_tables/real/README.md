# Real case CSVs

Drop per-case exports from real systems under `data_tables/real/<case_id>/*.csv`.

The gateway loads this folder when `--data-source real` is passed to `main.py`, or when `--data-source auto` picks this folder (highest priority when non-empty). Contents under case subfolders are gitignored; **never commit real customer data**.

Column headers must match `config/data_profiles/<table>.yaml`.
