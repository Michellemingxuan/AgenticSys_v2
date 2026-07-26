"""Regression guard: the conductor must NOT import the `server` module.

server.py runs its gateway build + `init_tools(...)` at MODULE TOP LEVEL. When
the app is launched via `python server.py`, that code runs under `__main__`.
If any runtime module does `import server`, Python loads a SECOND copy of the
module (as `server`, distinct from `__main__`), re-runs the bootstrap, and the
second `init_tools(new_gateway)` rebinds `tools.data_tools._gateway` to a fresh,
never-case-scoped gateway — silently breaking every live-data lookup
(`_resolve_real_table` sees `get_case_id()` is None → queries return None →
"table not found for current case").

A prior version fetched the Amem handle via `import server` inside
`_assemble_input`; it now reads it off `sess`. This test locks that in.
"""
import inspect

import runner.turn.conductor as conductor


def test_conductor_does_not_import_server_module():
    src = inspect.getsource(conductor)
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        # Skip comments — the file documents WHY it must not import server.
        if stripped.startswith("#"):
            continue
        if stripped.startswith("import server") or stripped.startswith("from server "):
            offenders.append((i, stripped))
    assert not offenders, (
        "conductor must not import the `server` module (re-runs bootstrap, "
        f"rebinds the data gateway to an unscoped instance): {offenders}"
    )
