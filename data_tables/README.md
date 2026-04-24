# `data_tables/` — per-case CSV source

Case-level CSV tables, split by source:

- `simulated/<case>/*.csv` — synthetic or hand-authored cases (safe to commit metadata, contents gitignored).
- `real/<case>/*.csv` — real exports from production systems (never commit contents; treat as sensitive).

`main.py` selects between these via the `--data-source` flag (default `auto`):

| Flag         | Behavior                                                                     |
| ------------ | ---------------------------------------------------------------------------- |
| `auto`       | `real/` if non-empty → else `simulated/` → else `generator`                  |
| `real`       | Use `real/` only; error out if empty                                         |
| `simulated`  | Use `simulated/` only; error out if empty                                    |
| `generator`  | Skip both folders; synthesise from `config/data_profiles/*.yaml`             |

The `data_source` event in the session log records which path was taken.

## Directory layout

```
data_tables/
├── simulated/
│   ├── CASE-00001/
│   │   ├── bureau.csv
│   │   ├── payments.csv
│   │   └── ...
│   └── CASE-00002/
│       └── ...
└── real/
    ├── CASE-RR-0001/
    │   ├── bureau.csv
    │   └── ...
    └── ...
```

One subdirectory per case, named exactly the case ID. One CSV per table, named exactly the table name. Column headers must match the schemas declared in `config/data_profiles/*.yaml` — the specialists query by those exact column names.

## Populating `simulated/`

Generate sample rows from the YAML profiles:

```bash
python -m datalayer --output data_tables/simulated --seed 42 --cases 3
```

That creates `data_tables/simulated/CASE-00001/`, `CASE-00002/`, `CASE-00003/` with full CSVs matching every schema.

## Populating `real/`

Export from your internal system, one file per table per case, into `data_tables/real/<case_id>/`. Column headers must match `config/data_profiles/<table>.yaml`. When in doubt, run the generator once to see the expected column names, then replace with real data.

## Data safety

Contents under `data_tables/*/` are `.gitignore`d so they never accidentally reach the remote. The READMEs themselves are tracked. Treat real CSVs as sensitive — the firewall + data manager redact identifiers on the LLM side, but the raw files on disk are unmasked.
