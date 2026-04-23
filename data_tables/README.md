# `data_tables/` — per-case CSV source for real-data runs

Drop per-case CSV files here to replace the in-memory synthetic generator. `main.py` auto-detects: if any case directories exist below this folder, the CSV gateway loads them; otherwise it falls back to `DataGenerator`.

## Directory layout

```
data_tables/
├── CASE-00001/
│   ├── bureau.csv
│   ├── payments.csv
│   ├── spends.csv
│   ├── txn_monthly.csv
│   ├── model_scores.csv
│   ├── wcc_flags.csv
│   ├── xbu_summary.csv
│   ├── cust_tenure.csv
│   └── income_dti.csv
├── CASE-00002/
│   └── ...
```

One subdirectory per case, named exactly the case ID. One CSV per table, named exactly the table name (without the `.csv` extension the table name would be `bureau`, `payments`, etc.). Column headers must match the schemas declared in `config/data_profiles/*.yaml` — the specialists query by those exact column names.

## Two ways to populate

### Option 1 — Generate sample data from the YAML profiles

Produces internally-consistent synthetic rows for every table. Useful for smoke testing the pipeline without real data:

```bash
python -m data --output data_tables/ --seed 42 --cases 3
```

That creates `data_tables/CASE-00001/`, `CASE-00002/`, `CASE-00003/` with full CSVs matching every schema.

### Option 2 — Drop real CSV exports

Export from your internal system one file per table per case. The column headers must match `config/data_profiles/<table>.yaml`. When in doubt, run Option 1 first to see the expected column names, then replace with real data.

## Data safety

This directory is `.gitignore`d (`data_tables/*/`) so contents never accidentally reach the remote. The `README.md` itself is tracked. Even so, treat real CSVs here as sensitive — the firewall + data manager redact 6+-digit runs and `CASE-\d+` tokens on the LLM side, but the raw files on disk are unmasked.

## How main.py picks the source

On startup `main.py` calls `SimulatedDataGateway.from_case_folders("data_tables")`. If that returns at least one case, it uses the CSVs; otherwise it falls back to `DataGenerator(seed, cases=50)`. The `data_source` event in the session log records which path was taken so you can confirm after the fact.
