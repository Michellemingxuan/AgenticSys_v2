# Simulated case CSVs

Drop per-case CSV exports from the generator (or hand-authored synthetic cases) under `data_tables/simulated/<case_id>/*.csv`.

The gateway loads this folder when `--data-source simulated` is passed to `main.py`, or when `--data-source auto` falls back here (real folder empty → this folder). Contents under case subfolders are gitignored.

Generate sample data:

```bash
python -m datalayer --output data_tables/simulated --seed 42 --cases 3
```
