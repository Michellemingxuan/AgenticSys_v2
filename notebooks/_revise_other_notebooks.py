"""Revise the remaining notebooks alongside the new test_chat_mode.ipynb.

- test_data_access: rewrite the legacy data-layer import in cell 3.
- test_compare_review, test_data_query, test_report_agent, test_team_construction,
  test_report_mode: prepend a deprecation cell pointing to test_chat_mode.

Run from project root:
    python notebooks/_revise_other_notebooks.py
"""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf

NB_DIR = Path(__file__).parent

# ─── 1. test_data_access — patch the bad import ─────────────────────────
def patch_data_access() -> None:
    path = NB_DIR / "test_data_access.ipynb"
    nb = nbf.read(path, as_version=4)
    patched = 0
    for c in nb["cells"]:
        if c["cell_type"] != "code":
            continue
        src = "".join(c["source"]) if isinstance(c["source"], list) else c["source"]
        if "from data.gateway" in src or "from data.catalog" in src:
            new = (
                src
                .replace("from data.gateway import SimulatedDataGateway",
                         "from datalayer.gateway import LocalDataGateway")
                .replace("SimulatedDataGateway.from_case_folders",
                         "LocalDataGateway.from_case_folders")
                .replace("from data.catalog import DataCatalog",
                         "from datalayer.catalog import DataCatalog")
                # The DataCatalog constructor no longer takes profile_dir
                .replace("DataCatalog(profile_dir=PROFILE_DIR)", "DataCatalog()")
            )
            c["source"] = new.splitlines(keepends=True)
            patched += 1
    nbf.write(nb, path)
    print(f"patched {patched} cell(s) in {path.name}")


# ─── 2. Deprecation banner for legacy notebooks ─────────────────────────
DEPRECATION_BANNER = """\
> ⚠️ **Legacy notebook — superseded by `test_chat_mode.ipynb`**
>
> This notebook tested a Python-level orchestration step that no longer exists
> after the migration to the OpenAI Agents SDK (commit `762f7a9`, April 2026).
> Under the A1 design, the orchestrator agent runs as a single `Runner.run` and
> the legacy classes / functions referenced below have been deleted.
>
> **For the current workflow** — including intermediate results (questions
> after `relevance_check`, sub-questions to specialists, general_specialist's
> review, etc.) — see [`test_chat_mode.ipynb`](test_chat_mode.ipynb), which
> exercises the production pipeline and exposes every stage via
> `result.new_items`.
>
> **Why this notebook is kept**: as historical reference and as a starting
> template if you want to write a focused iteration notebook for a single
> agent factory (`build_specialist_agent`, `build_report_agent`,
> `build_general_specialist`, `build_orchestrator_agent`) under the new SDK.
"""


def add_banner(nb_name: str) -> None:
    path = NB_DIR / f"{nb_name}.ipynb"
    nb = nbf.read(path, as_version=4)

    if nb["cells"]:
        first = nb["cells"][0]
        first_src = "".join(first["source"]) if isinstance(first["source"], list) else first["source"]
        if "Legacy notebook" in first_src:
            print(f"banner already present in {path.name}; skipping")
            return

    banner = nbf.v4.new_markdown_cell(DEPRECATION_BANNER)
    nb["cells"].insert(0, banner)
    nbf.write(nb, path)
    print(f"prepended deprecation banner to {path.name}")


if __name__ == "__main__":
    patch_data_access()
    for name in (
        "test_compare_review",
        "test_data_query",
        "test_report_agent",
        "test_team_construction",
        "test_report_mode",
    ):
        add_banner(name)
