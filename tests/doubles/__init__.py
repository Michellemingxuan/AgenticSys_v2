"""Test doubles — stand-ins for services this repo calls but does not own.

Importable at runtime as well as from the suite (`pythonpath = ["."]`), so a
dev deployment can point at one deliberately: e.g.
`KNOWLEDGE_BASE_CLIENT=tests.doubles.knowledge_base_sim:answer_question`.
They live here, not in `tools/`, because nothing in this package is callable by
an agent — `tools/` is the tool surface.
"""
